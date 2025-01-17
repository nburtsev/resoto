from typing import Dict, List

from pytest import fixture
from resotocore.cli.cli import CLIService
from resotocore.config import ConfigEntity
from resotocore.db.model import QueryModel
from resotocore.ids import ConfigId, GraphName
from resotocore.query.model import P, Query
from resotocore.report import (
    BenchmarkConfigRoot,
    CheckConfigRoot,
    BenchmarkResult,
    Benchmark,
    ReportCheck,
)
from resotocore.report.inspector_service import InspectorService
from resotocore.report.report_config import (
    config_model,
    ReportCheckCollectionConfig,
    BenchmarkConfig,
)
from resotocore.util import partition_by


@fixture
async def inspector_service_with_test_benchmark(
    cli: CLIService, inspection_checks: List[ReportCheck], benchmark: Benchmark
) -> InspectorService:
    service = InspectorService(cli)
    for check in inspection_checks:
        await service.update_check(check)
    await service.update_benchmark(benchmark)
    return service


async def test_config_model() -> None:
    models = config_model()
    assert len(models) == 7


async def test_list_inspect_checks(inspector_service: InspectorService) -> None:
    # list all available checks
    all_checks = {i.id: i for i in await inspector_service.list_checks()}
    assert len(all_checks) >= 30

    # use different filter options. The more filter are used, fewer results are returned
    filter_options = dict(
        provider="aws",
        service="ec2",
        category="security",
        kind="aws_ec2_instance",
        check_ids=["aws_ec2_internet_facing_with_instance_profile"],
    )
    last_len = len(all_checks)
    for options in range(1, len(filter_options)):
        args = dict(list(filter_options.items())[0:options])
        matching_checks = [i for i in await inspector_service.list_checks(**args)]  # type: ignore
        assert len(matching_checks) > 0
        assert len(matching_checks) <= last_len
        last_len = len(matching_checks)
    assert last_len < len(all_checks)


async def test_perform_benchmark(inspector_service: InspectorService) -> None:
    def assert_result(results: Dict[str, BenchmarkResult]) -> None:
        result = results["test"]
        assert result.children[0].checks[0].number_of_resources_failing == 10
        assert result.children[1].checks[0].number_of_resources_failing == 10
        filtered = result.filter_result(filter_failed=True)
        assert filtered.children[0].checks[0].number_of_resources_failing == 10
        assert len(filtered.children[0].checks[0].resources_failing_by_account["sub_root"]) == 10
        assert filtered.children[1].checks[0].number_of_resources_failing == 10
        assert len(filtered.children[1].checks[0].resources_failing_by_account["sub_root"]) == 10
        passing, failing = result.passing_failing_checks_for_account("sub_root")
        assert len(passing) == 0
        assert len(failing) == 2
        passing, failing = result.passing_failing_checks_for_account("does_not_exist")
        assert len(passing) == 2
        assert len(failing) == 0

    graph_name = GraphName(inspector_service.cli.env["graph"])
    performed = await inspector_service.perform_benchmarks(graph_name, ["test"], sync_security_section=True)
    assert_result(performed)

    # make sure the result is persisted as part of the node
    async def count_vulnerable() -> int:
        db = inspector_service.db_access.get_graph_db(graph_name)
        model = await inspector_service.model_handler.load_model(graph_name)
        all_vunerable = Query.by(P("security.has_issues") == True)  # noqa
        async with await db.search_list(QueryModel(all_vunerable, model), with_count=True) as cursor:
            return cursor.count()  # type: ignore

    assert await count_vulnerable() == 10

    # loading the result from the db should give the same information
    loaded = await inspector_service.load_benchmarks(graph_name, ["test"])
    assert_result(loaded)


async def test_benchmark_node_result(inspector_service: InspectorService) -> None:
    results = await inspector_service.perform_benchmarks(inspector_service.cli.env["graph"], ["test"])
    result = results["test"]
    node_edge_list = result.to_graph()
    nodes, edges = partition_by(lambda x: x["type"] == "node", node_edge_list)
    assert len(node_edge_list) == 9  # 1 benchmark, 2 collections, 2 checks, 4 edges
    assert len(nodes) == 5
    assert len(edges) == 4


async def test_predefined_checks(inspector_service: InspectorService) -> None:
    checks = ReportCheckCollectionConfig.from_files()
    assert len(checks) > 0
    for name, check in checks.items():
        validation = await inspector_service.validate_check_collection_config({CheckConfigRoot: check})
        assert validation is None, str(validation)


async def test_predefined_benchmarks(inspector_service: InspectorService) -> None:
    benchmarks = BenchmarkConfig.from_files()
    assert len(benchmarks) > 0
    for name, check in benchmarks.items():
        config = {BenchmarkConfigRoot: check}
        cfg_id = ConfigId(name)
        validation = await inspector_service.validate_benchmark_config(cfg_id, config)
        assert validation is None, f"Benchmark: {name}" + str(validation)
        benchmark = BenchmarkConfig.from_config(ConfigEntity(cfg_id, config))
        assert benchmark.clouds == ["aws"]


async def test_list_failing(inspector_service: InspectorService) -> None:
    graph = inspector_service.cli.env["graph"]
    search_res = [r async for r in await inspector_service.list_failing_resources(graph, "test_test_search")]
    assert len(search_res) == 10
    cmd_res = [r async for r in await inspector_service.list_failing_resources(graph, "test_test_cmd")]
    assert len(cmd_res) == 10
    search_res_account = [
        r async for r in await inspector_service.list_failing_resources(graph, "test_test_search", ["n/a"])
    ]
    assert len(search_res_account) == 0
    cmd_res_account = [r async for r in await inspector_service.list_failing_resources(graph, "test_test_cmd", ["n/a"])]
    assert len(cmd_res_account) == 0


async def test_file_inspector(inspector_service: InspectorService) -> None:
    assert len(await inspector_service.list_benchmarks()) >= 1
    assert len(await inspector_service.filter_checks()) >= 100
    aws_cis = await inspector_service.benchmark("aws_cis_1_5")
    assert aws_cis is not None
