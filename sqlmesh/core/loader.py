from __future__ import annotations

import abc
import linecache
import logging
import os
import typing as t
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlglot.errors import SchemaError, SqlglotError
from sqlglot.schema import MappingSchema
from sqlglot import exp, Dialect


from sqlmesh.core import constants as c
from sqlmesh.core.audit import Audit, load_multiple_audits
from sqlmesh.core.dialect import parse, MACRO_BEGIN
from sqlmesh.core.macros import (
    MacroRegistry,
    macro,
    _norm_var_arg_lambda,
    normalize_macro_name,
    MacroEvaluator,
)
from sqlmesh.core.metric import Metric, MetricMeta, expand_metrics, load_metric_ddl
from sqlmesh.core.model import (
    Model,
    ModelCache,
    OptimizedQueryCache,
    SeedModel,
    create_external_model,
    load_sql_based_model,
)
from sqlmesh.core.model import model as model_registry
from sqlmesh.utils import UniqueKeyDict
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.jinja import JinjaMacroRegistry, MacroExtractor
from sqlmesh.utils.metaprogramming import import_python_file
from sqlmesh.utils.yaml import YAML

if t.TYPE_CHECKING:
    from sqlmesh.core.config import Config
    from sqlmesh.core.context import GenericContext


logger = logging.getLogger(__name__)


# TODO: consider moving this to context
def update_model_schemas(
    dag: DAG[str],
    models: UniqueKeyDict[str, Model],
    context_path: Path,
) -> None:
    schema = MappingSchema(normalize=False)
    optimized_query_cache: OptimizedQueryCache = OptimizedQueryCache(context_path / c.CACHE)

    for name in dag.sorted:
        model = models.get(name)

        # External models don't exist in the context, so we need to skip them
        if not model:
            continue

        try:
            model.update_schema(schema)
            optimized_query_cache.with_optimized_query(model)

            columns_to_types = model.columns_to_types
            if columns_to_types is not None:
                schema.add_table(
                    model.fqn, columns_to_types, dialect=model.dialect, normalize=False
                )
        except SchemaError as e:
            if "nesting level:" in str(e):
                logger.error(
                    "SQLMesh requires all model names and references to have the same level of nesting."
                )
            raise


@dataclass
class LoadedProject:
    macros: MacroRegistry
    jinja_macros: JinjaMacroRegistry
    models: UniqueKeyDict[str, Model]
    audits: UniqueKeyDict[str, Audit]
    metrics: UniqueKeyDict[str, Metric]
    dag: DAG[str]


class Loader(abc.ABC):
    """Abstract base class to load macros and models for a context"""

    def __init__(self) -> None:
        self._path_mtimes: t.Dict[Path, float] = {}
        self._dag: DAG[str] = DAG()

    def load(self, context: GenericContext, update_schemas: bool = True) -> LoadedProject:
        """
        Loads all macros and models in the context's path.

        Args:
            context: The context to load macros and models for.
            update_schemas: Convert star projections to explicit columns.
        """
        # python files are cached by the system
        # need to manually clear here so we can reload macros
        linecache.clearcache()

        self._context = context
        self._path_mtimes.clear()
        self._dag = DAG()

        config_mtimes: t.Dict[Path, t.List[float]] = defaultdict(list)
        for context_path, config in self._context.configs.items():
            for config_file in context_path.glob("config.*"):
                self._track_file(config_file)
                config_mtimes[context_path].append(self._path_mtimes[config_file])

        for config_file in c.SQLMESH_PATH.glob("config.*"):
            self._track_file(config_file)
            config_mtimes[c.SQLMESH_PATH].append(self._path_mtimes[config_file])

        self._config_mtimes = {path: max(mtimes) for path, mtimes in config_mtimes.items()}

        macros, jinja_macros = self._load_scripts()
        models = self._load_models(macros, jinja_macros)

        for model in models.values():
            self._add_model_to_dag(model)

        if update_schemas:
            update_model_schemas(
                self._dag,
                models,
                self._context.path,
            )
            for model in models.values():
                # The model definition can be validated correctly only after the schema is set.
                model.validate_definition()

        metrics = self._load_metrics()

        project = LoadedProject(
            macros=macros,
            jinja_macros=jinja_macros,
            models=models,
            audits=self._load_audits(macros=macros, jinja_macros=jinja_macros),
            metrics=expand_metrics(metrics),
            dag=self._dag,
        )
        return project

    def reload_needed(self) -> bool:
        """
        Checks for any modifications to the files the macros and models depend on
        since the last load.

        Returns:
            True if a modification is found; False otherwise
        """
        return any(
            not path.exists() or path.stat().st_mtime > initial_mtime
            for path, initial_mtime in self._path_mtimes.items()
        )

    @abc.abstractmethod
    def _load_scripts(self) -> t.Tuple[MacroRegistry, JinjaMacroRegistry]:
        """Loads all user defined macros."""

    @abc.abstractmethod
    def _load_models(
        self, macros: MacroRegistry, jinja_macros: JinjaMacroRegistry
    ) -> UniqueKeyDict[str, Model]:
        """Loads all models."""

    @abc.abstractmethod
    def _load_audits(
        self, macros: MacroRegistry, jinja_macros: JinjaMacroRegistry
    ) -> UniqueKeyDict[str, Audit]:
        """Loads all audits."""

    def _load_metrics(self) -> UniqueKeyDict[str, MetricMeta]:
        return UniqueKeyDict("metrics")

    def _load_external_models(self) -> UniqueKeyDict[str, Model]:
        models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
        for context_path, config in self._context.configs.items():
            external_models_yaml = Path(context_path / c.EXTERNAL_MODELS_YAML)
            deprecated_yaml = Path(context_path / c.EXTERNAL_MODELS_DEPRECATED_YAML)
            external_models_path = context_path / c.EXTERNAL_MODELS

            paths_to_load = []
            if external_models_yaml.exists():
                paths_to_load.append(external_models_yaml)
            elif deprecated_yaml.exists():
                paths_to_load.append(deprecated_yaml)

            if external_models_path.exists() and external_models_path.is_dir():
                paths_to_load.extend(external_models_path.glob("*.yaml"))

            for path in paths_to_load:
                self._track_file(path)

                with open(path, "r", encoding="utf-8") as file:
                    for row in YAML().load(file.read()):
                        model = create_external_model(
                            **row,
                            dialect=config.model_defaults.dialect,
                            defaults=config.model_defaults.dict(),
                            path=path,
                            project=config.project,
                            default_catalog=self._context.default_catalog,
                        )
                        models[model.fqn] = model
        return models

    def _add_model_to_dag(self, model: Model) -> None:
        self._dag.add(model.fqn, model.depends_on)

    def _track_file(self, path: Path) -> None:
        """Project file to track for modifications"""
        self._path_mtimes[path] = path.stat().st_mtime


class SqlMeshLoader(Loader):
    """Loads macros and models for a context using the SQLMesh file formats"""

    def _load_scripts(self) -> t.Tuple[MacroRegistry, JinjaMacroRegistry]:
        """Loads all user defined macros."""
        # Store a copy of the macro registry
        standard_macros = macro.get_registry()
        jinja_macros = JinjaMacroRegistry()
        extractor = MacroExtractor()

        macros_max_mtime: t.Optional[float] = None

        for context_path, config in self._context.configs.items():
            for path in self._glob_paths(context_path / c.MACROS, config=config, extension=".py"):
                if import_python_file(path, context_path):
                    self._track_file(path)
                    macro_file_mtime = self._path_mtimes[path]
                    macros_max_mtime = (
                        max(macros_max_mtime, macro_file_mtime)
                        if macros_max_mtime
                        else macro_file_mtime
                    )

            for path in self._glob_paths(context_path / c.MACROS, config=config, extension=".sql"):
                self._track_file(path)
                macro_file_mtime = self._path_mtimes[path]
                macros_max_mtime = (
                    max(macros_max_mtime, macro_file_mtime)
                    if macros_max_mtime
                    else macro_file_mtime
                )
                with open(path, "r", encoding="utf-8") as file:
                    sql_file = file.read()
                    tokens = Dialect().tokenizer.tokenize(sql_file)

                    if tokens[0].text == MACRO_BEGIN:
                        parsed = parse(sql=sql_file, tokens=tokens)
                        for macro_func in parsed:
                            macro_name = macro_func.this
                            lambda_func = exp.Lambda(
                                this=macro_func.expression[0], expressions=macro_func.expressions
                            )
                            _, fn = _norm_var_arg_lambda(MacroEvaluator(), lambda_func)
                            standard_macros[macro_name] = lambda _, *args: fn(
                                args[0] if len(args) == 1 else exp.Tuple(expressions=list(args))
                            )
                            macro(normalize_macro_name(macro_name))(standard_macros[macro_name])
                    else:
                        jinja_macros.add_macros(extractor.extract(jinja=sql_file, tokens=tokens))

        self._macros_max_mtime = macros_max_mtime

        macros = macro.get_registry()
        macro.set_registry(standard_macros)

        return macros, jinja_macros

    def _load_models(
        self, macros: MacroRegistry, jinja_macros: JinjaMacroRegistry
    ) -> UniqueKeyDict[str, Model]:
        """
        Loads all of the models within the model directory with their associated
        audits into a Dict and creates the dag
        """
        models = self._load_sql_models(macros, jinja_macros)
        models.update(self._load_external_models())
        models.update(self._load_python_models())

        return models

    def _load_sql_models(
        self, macros: MacroRegistry, jinja_macros: JinjaMacroRegistry
    ) -> UniqueKeyDict[str, Model]:
        """Loads the sql models into a Dict"""
        models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
        for context_path, config in self._context.configs.items():
            cache = SqlMeshLoader._Cache(self, context_path)
            variables = self._variables(config)

            for path in self._glob_paths(context_path / c.MODELS, config=config, extension=".sql"):
                if not os.path.getsize(path):
                    continue

                self._track_file(path)

                def _load() -> Model:
                    with open(path, "r", encoding="utf-8") as file:
                        try:
                            expressions = parse(
                                file.read(), default_dialect=config.model_defaults.dialect
                            )
                        except SqlglotError as ex:
                            raise ConfigError(
                                f"Failed to parse a model definition at '{path}': {ex}."
                            )

                    return load_sql_based_model(
                        expressions,
                        defaults=config.model_defaults.dict(),
                        macros=macros,
                        jinja_macros=jinja_macros,
                        path=Path(path).absolute(),
                        module_path=context_path,
                        dialect=config.model_defaults.dialect,
                        time_column_format=config.time_column_format,
                        physical_schema_override=config.physical_schema_override,
                        project=config.project,
                        default_catalog=self._context.default_catalog,
                        variables=variables,
                        infer_names=config.model_naming.infer_names,
                    )

                model = cache.get_or_load_model(path, _load)
                models[model.fqn] = model

                if isinstance(model, SeedModel):
                    seed_path = model.seed_path
                    self._track_file(seed_path)

        return models

    def _load_python_models(self) -> UniqueKeyDict[str, Model]:
        """Loads the python models into a Dict"""
        models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
        registry = model_registry.registry()
        registry.clear()
        registered: t.Set[str] = set()

        for context_path, config in self._context.configs.items():
            variables = self._variables(config)
            model_registry._dialect = config.model_defaults.dialect
            try:
                for path in self._glob_paths(
                    context_path / c.MODELS, config=config, extension=".py"
                ):
                    if not os.path.getsize(path):
                        continue

                    self._track_file(path)
                    import_python_file(path, context_path)
                    new = registry.keys() - registered
                    registered |= new
                    for name in new:
                        model = registry[name].model(
                            path=path,
                            module_path=context_path,
                            defaults=config.model_defaults.dict(),
                            dialect=config.model_defaults.dialect,
                            time_column_format=config.time_column_format,
                            physical_schema_override=config.physical_schema_override,
                            project=config.project,
                            default_catalog=self._context.default_catalog,
                            variables=variables,
                            infer_names=config.model_naming.infer_names,
                        )
                        models[model.fqn] = model
            finally:
                model_registry._dialect = None

        return models

    def _load_audits(
        self, macros: MacroRegistry, jinja_macros: JinjaMacroRegistry
    ) -> UniqueKeyDict[str, Audit]:
        """Loads all the model audits."""
        audits_by_name: UniqueKeyDict[str, Audit] = UniqueKeyDict("audits")
        for context_path, config in self._context.configs.items():
            variables = self._variables(config)
            for path in self._glob_paths(context_path / c.AUDITS, config=config, extension=".sql"):
                self._track_file(path)
                with open(path, "r", encoding="utf-8") as file:
                    expressions = parse(file.read(), default_dialect=config.model_defaults.dialect)
                    audits = load_multiple_audits(
                        expressions=expressions,
                        path=path,
                        module_path=context_path,
                        macros=macros,
                        jinja_macros=jinja_macros,
                        dialect=config.model_defaults.dialect,
                        default_catalog=self._context.default_catalog,
                        variables=variables,
                    )
                    for audit in audits:
                        audits_by_name[audit.name] = audit
        return audits_by_name

    def _load_metrics(self) -> UniqueKeyDict[str, MetricMeta]:
        """Loads all metrics."""
        metrics: UniqueKeyDict[str, MetricMeta] = UniqueKeyDict("metrics")

        for context_path, config in self._context.configs.items():
            for path in self._glob_paths(context_path / c.METRICS, config=config, extension=".sql"):
                if not os.path.getsize(path):
                    continue
                self._track_file(path)

                with open(path, "r", encoding="utf-8") as file:
                    dialect = config.model_defaults.dialect
                    try:
                        for expression in parse(file.read(), default_dialect=dialect):
                            metric = load_metric_ddl(expression, path=path, dialect=dialect)
                            metrics[metric.name] = metric
                    except SqlglotError as ex:
                        raise ConfigError(f"Failed to parse metric definitions at '{path}': {ex}.")

        return metrics

    def _glob_paths(
        self, path: Path, config: Config, extension: str
    ) -> t.Generator[Path, None, None]:
        """
        Globs the provided path for the file extension but also removes any filepaths that match an ignore
        pattern either set in constants or provided in config

        Args:
            path: The filepath to glob
            extension: The extension to check for in that path (checks recursively in zero or more subdirectories)

        Returns:
            Matched paths that are not ignored
        """
        for filepath in path.glob(f"**/*{extension}"):
            for ignore_pattern in config.ignore_patterns:
                if filepath.match(ignore_pattern):
                    break
            else:
                yield filepath

    def _variables(self, config: Config) -> t.Dict[str, t.Any]:
        gateway_name = self._context.gateway or self._context.config.default_gateway_name
        try:
            gateway = config.get_gateway(gateway_name)
        except ConfigError:
            logger.warning("Gateway '%s' not found in project '%s'", gateway_name, config.project)
            gateway = None
        return {
            **config.variables,
            **(gateway.variables if gateway else {}),
            c.GATEWAY: gateway_name,
        }

    class _Cache:
        def __init__(self, loader: SqlMeshLoader, context_path: Path):
            self._loader = loader
            self._context_path = context_path
            self._model_cache = ModelCache(self._context_path / c.CACHE)

        def get_or_load_model(self, target_path: Path, loader: t.Callable[[], Model]) -> Model:
            model = self._model_cache.get_or_load(
                self._cache_entry_name(target_path),
                self._model_cache_entry_id(target_path),
                loader=loader,
            )
            model._path = target_path
            return model

        def _cache_entry_name(self, target_path: Path) -> str:
            return "__".join(target_path.relative_to(self._context_path).parts).replace(
                target_path.suffix, ""
            )

        def _model_cache_entry_id(self, model_path: Path) -> str:
            mtimes = [
                self._loader._path_mtimes[model_path],
                self._loader._macros_max_mtime,
                self._loader._config_mtimes.get(self._context_path),
                self._loader._config_mtimes.get(c.SQLMESH_PATH),
            ]
            return "__".join(
                [
                    str(max(m for m in mtimes if m is not None)),
                    self._loader._context.config.fingerprint,
                    # We need to check default catalog since the provided config could not change but the
                    # gateway we are using could change, therefore potentially changing the default catalog
                    # which would then invalidate the cached model definition.
                    self._loader._context.default_catalog or "",
                ]
            )
