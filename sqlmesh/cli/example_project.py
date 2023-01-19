import typing as t
from enum import Enum
from pathlib import Path

import click

DEFAULT_CONFIG = """import duckdb
from sqlmesh.core.config import Config, DuckDBConnectionConfig


config = Config(
    connections=DuckDBConnectionConfig(database="db.duckdb")
)


test_config = Config(
    connections=DuckDBConnectionConfig(),
)
"""


DEFAULT_AIRFLOW_CONFIG = """import duckdb
from sqlmesh.core.config import AirflowSchedulerConfig, Config

config = Config(
    scheduler=AirflowSchedulerConfig(
        airflow_url="http://localhost:8080/",
        username="airflow",
        password="airflow",
    ),
)


test_config = Config(
    connections= DuckDBConnectionConfig(),
)
"""

EXAMPLE_SCHEMA_NAME = "sqlmesh_example"
EXAMPLE_FULL_MODEL_NAME = f"{EXAMPLE_SCHEMA_NAME}.example_full_model"
EXAMPLE_INCREMENTAL_MODEL_NAME = f"{EXAMPLE_SCHEMA_NAME}.example_incremental_model"


EXAMPLE_FULL_MODEL_DEF = f"""MODEL (
  name {EXAMPLE_FULL_MODEL_NAME},
  kind FULL,
  cron '@daily',
);

SELECT
  item_id,
  count(distinct id) AS num_orders,
FROM
    {EXAMPLE_INCREMENTAL_MODEL_NAME}
GROUP BY item_id
"""

EXAMPLE_INCREMENTAL_MODEL_DEF = f"""MODEL (
    name {EXAMPLE_INCREMENTAL_MODEL_NAME},
    kind INCREMENTAL_BY_TIME_RANGE (
        time_column ds
    ),
    start '2020-01-01',
    cron '@daily',
);

SELECT
    id,
    item_id,
    ds,
FROM
    (VALUES
        (1, 1, '2020-01-01'),
        (1, 2, '2020-01-01'),
        (2, 1, '2020-01-01'),
        (3, 3, '2020-01-03'),
        (4, 1, '2020-01-04'),
        (5, 1, '2020-01-05'),
        (6, 1, '2020-01-06'),
        (7, 1, '2020-01-07')
    ) AS t (id, item_id, ds)
WHERE
    ds between @start_ds and @end_ds
"""

EXAMPLE_AUDIT = f"""AUDIT (
  name asset_positive_order_ids,
  model {EXAMPLE_FULL_MODEL_NAME}
);

SELECT *
FROM {EXAMPLE_FULL_MODEL_NAME}
WHERE
  item_id < 0
"""


EXAMPLE_TEST = f"""test_example_full_model:
  model: {EXAMPLE_FULL_MODEL_NAME}
  inputs:
    {EXAMPLE_INCREMENTAL_MODEL_NAME}:
        rows:
        - id: 1
          item_id: 1
          ds: '2020-01-01'
        - id: 2
          item_id: 1
          ds: '2020-01-02'
        - id: 3
          item_id: 2
          ds: '2020-01-03'
  outputs:
    query:
      rows:
      - item_id: 1
        num_orders: 2
      - item_id: 2
        num_orders: 1
"""


class ProjectTemplate(Enum):
    AIRFLOW = "airflow"
    DEFAULT = "default"


def init_example_project(
    path: t.Union[str, Path], template: ProjectTemplate = ProjectTemplate.DEFAULT
) -> None:
    root_path = Path(path)
    config_path = root_path / "config.py"
    audits_path = root_path / "audits"
    macros_path = root_path / "macros"
    models_path = root_path / "models"
    tests_path = root_path / "tests"

    if config_path.exists():
        raise click.ClickException(f"Found an existing config in '{config_path}'")

    _create_folders([audits_path, macros_path, models_path, tests_path])
    _create_config(config_path, template)
    _create_audits(audits_path)
    _create_models(models_path)
    _create_tests(tests_path)


def _create_folders(target_folders: t.Sequence[Path]) -> None:
    for folder_path in target_folders:
        folder_path.mkdir()
        (folder_path / ".gitkeep").touch()


def _create_config(config_path: Path, template: ProjectTemplate) -> None:
    _write_file(
        config_path,
        DEFAULT_AIRFLOW_CONFIG
        if template == ProjectTemplate.AIRFLOW
        else DEFAULT_CONFIG,
    )


def _create_audits(audits_path: Path) -> None:
    _write_file(audits_path / "example_full_model.sql", EXAMPLE_AUDIT)


def _create_models(models_path: Path) -> None:
    for model_name, model_def in [
        (EXAMPLE_FULL_MODEL_NAME, EXAMPLE_FULL_MODEL_DEF),
        (EXAMPLE_INCREMENTAL_MODEL_NAME, EXAMPLE_INCREMENTAL_MODEL_DEF),
    ]:
        _write_file(models_path / f"{model_name.split('.')[-1]}.sql", model_def)


def _create_tests(tests_path: Path) -> None:
    _write_file(tests_path / "test_example_full_model.yaml", EXAMPLE_TEST)


def _write_file(path: Path, payload: str) -> None:
    with open(path, "w", encoding="utf-8") as fd:
        fd.write(payload)
