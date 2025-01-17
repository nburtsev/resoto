import logging
from collections import defaultdict
from functools import lru_cache
from typing import Optional, List, Dict, Tuple, Callable, AsyncIterator, cast

from aiostream import stream, pipe
from aiostream.core import Stream
from attr import define
from resotocore.analytics import CoreEvent
from resotocore.cli import list_sink
from resotocore.cli.model import CLIContext, CLI
from resotocore.config import ConfigEntity, ConfigHandler
from resotocore.db.model import QueryModel
from resotocore.db.reportdb import ReportCheckDb, BenchmarkDb
from resotocore.error import NotFoundError
from resotocore.ids import ConfigId, GraphName, NodeId
from resotocore.model.model import Model
from resotocore.model.resolve_in_graph import NodePath
from resotocore.model.typed_model import from_js
from resotocore.query.model import Query, P, Term
from resotocore.report import (
    Inspector,
    ReportCheck,
    Benchmark,
    BenchmarkResult,
    CheckCollection,
    CheckCollectionResult,
    CheckResult,
    CheckConfigRoot,
    ResotoReportConfig,
    BenchmarkConfigRoot,
    ResotoReportBenchmark,
    ResotoReportCheck,
    ReportSeverity,
    ReportConfigRoot,
    SecurityIssue,
)
from resotocore.report.report_config import ReportCheckCollectionConfig, BenchmarkConfig, ReportConfig
from resotocore.service import Service
from resotocore.types import Json
from resotocore.util import value_in_path, uuid_str, value_in_path_get
from resotolib.json_bender import Bender, S, bend

log = logging.getLogger(__name__)

SingleCheckResult = Dict[str, List[Json]]


@define
class CheckContext:
    accounts: Optional[List[str]] = None
    severity: Optional[ReportSeverity] = None
    only_failed: bool = False
    parallel_checks: int = 10

    def severities_including(self, severity: ReportSeverity) -> List[ReportSeverity]:
        return [s for s in ReportSeverity if self.includes_severity(severity)]

    def includes_severity(self, severity: ReportSeverity) -> bool:
        if self.severity is None:
            return True
        else:
            return self.severity.prio() <= severity.prio()


# This defines the subset of the data provided for every resource
ReportResourceData: Dict[str, Bender] = {
    "node_id": S("id"),
    "id": S("reported", "id"),
    "name": S("reported", "name"),
    "kind": S("reported", "kind"),
    "tags": S("reported", "tags"),
    "ctime": S("reported", "ctime"),
    "atime": S("reported", "atime"),
    "mtime": S("reported", "mtime"),
    "cloud": S("ancestors", "cloud", "reported", "name"),
    "account": S("ancestors", "account", "reported", "name"),
    "region": S("ancestors", "region", "reported", "name"),
    "zone": S("ancestors", "zone", "reported", "name"),
}


class InspectorService(Inspector, Service):
    def __init__(self, cli: CLI) -> None:
        super().__init__()
        self.db_access = cli.dependencies.db_access
        self.cli = cli
        self.template_expander = cli.dependencies.template_expander
        self.model_handler = cli.dependencies.model_handler
        self.event_sender = cli.dependencies.event_sender
        self.report_check_db: ReportCheckDb = cli.dependencies.db_access.report_check_db
        self.benchmark_db: BenchmarkDb = cli.dependencies.db_access.benchmark_db
        self.config_handler: ConfigHandler = cli.dependencies.config_handler

    async def report_config(self) -> ReportConfig:
        try:
            if (c := await self.config_handler.get_config(ResotoReportConfig)) and (
                v := c.config.get(ReportConfigRoot)
            ):
                return from_js(v, ReportConfig)
        except Exception as e:
            log.warning(f"Can not load report config: {e}")
        return ReportConfig()  # safe default

    async def list_benchmarks(self) -> List[Benchmark]:
        result = {b.id: b for b in benchmarks_from_file().values()}
        async for b in self.benchmark_db.all():
            result[b.id] = b
        return list(result.values())

    async def benchmark(self, bid: str) -> Optional[Benchmark]:
        if in_db := await self.benchmark_db.get(bid):
            return in_db
        elif in_file := benchmarks_from_file().get(bid):
            return in_file
        else:
            return None

    async def delete_benchmark(self, bid: str) -> None:
        if bid in benchmarks_from_file():
            raise ValueError(f"Deleting a predefined benchmark is not allowed: {bid}")
        await self.benchmark_db.delete(bid)

    async def update_benchmark(self, benchmark: Benchmark) -> Benchmark:
        if benchmark.id in benchmarks_from_file():
            raise ValueError(f"Changing a predefined benchmark is not allowed: {benchmark.id}")
        if invalid := await self.__validate_benchmark(benchmark):
            raise ValueError(f"Benchmark {benchmark.id} is invalid: {', '.join(invalid)}")
        return await self.benchmark_db.update(benchmark)

    async def delete_check(self, check_id: str) -> None:
        await self.report_check_db.delete(check_id)

    async def update_check(self, check: ReportCheck) -> ReportCheck:
        if invalid := await self.__validate_check(check):
            raise ValueError(f"Check {check.id} is invalid: {', '.join(invalid)}")
        return await self.report_check_db.update(check)

    async def __benchmarks(self, names: List[str]) -> Dict[str, Benchmark]:
        result: Dict[str, Benchmark] = {}
        for name in names:
            if b := await self.benchmark(name):
                result[name] = b
        return result

    async def list_checks(
        self,
        *,
        provider: Optional[str] = None,
        service: Optional[str] = None,
        category: Optional[str] = None,
        kind: Optional[str] = None,
        check_ids: Optional[List[str]] = None,
        context: Optional[CheckContext] = None,
        ignore_checks: Optional[List[str]] = None,
    ) -> List[ReportCheck]:
        def inspection_matches(inspection: ReportCheck) -> bool:
            return (
                (provider is None or provider == inspection.provider)
                and (service is None or service == inspection.service)
                and (category is None or category in inspection.categories)
                and (kind is None or kind in inspection.result_kinds)
                and (check_ids is None or inspection.id in check_ids)
                and (context is None or context.includes_severity(inspection.severity))
                and (ignore_checks is None or inspection.id not in ignore_checks)
            )

        return await self.filter_checks(inspection_matches)

    async def load_benchmarks(
        self,
        graph: GraphName,
        benchmark_names: List[str],
        *,
        accounts: Optional[List[str]] = None,
        severity: Optional[ReportSeverity] = None,
        only_failing: bool = False,
    ) -> Dict[str, BenchmarkResult]:
        context = CheckContext(accounts=accounts, severity=severity, only_failed=only_failing)
        config = await self.report_config()
        # create query
        term: Term = P("benchmarks[]").is_in(benchmark_names)
        # TODO: 17.01.2024: remove the next line after deployed on prd
        term = term.or_term(P("benchmark").is_in(benchmark_names))
        if severity:
            term = term & P("severity").is_in([s.value for s in context.severities_including(severity)])
        term = P.context("security.issues[]", term)
        if accounts:
            term = term & P("ancestors.account.reported.id").is_in(accounts)
        term = term & P("security.has_issues").eq(True)
        model = QueryModel(Query.by(term), await self.model_handler.load_model(graph))

        # collect all checks
        benchmarks = await self.__benchmarks(benchmark_names)
        check_ids = {check for b in benchmarks.values() for check in b.nested_checks() if config.check_allowed(check)}
        checks = await self.list_checks(check_ids=list(check_ids), context=context)
        check_lookup = {check.id: check for check in checks}

        # perform query, map resources and create lookup map
        check_results: Dict[str, SingleCheckResult] = defaultdict(lambda: defaultdict(list))
        async with await self.db_access.get_graph_db(graph).search_list(model) as cursor:
            async for entry in cursor:
                if account_id := value_in_path(entry, NodePath.ancestor_account_id):
                    mapped = bend(ReportResourceData, entry)
                    for issue in value_in_path_get(entry, NodePath.security_issues, cast(List[Json], [])):
                        if check := issue.get("check"):
                            check_results[check][account_id].append(mapped)
        return {
            name: self.__to_result(benchmark, check_lookup, check_results, context)
            for name, benchmark in benchmarks.items()
        }

    async def perform_benchmarks(
        self,
        graph: GraphName,
        benchmark_names: List[str],
        *,
        accounts: Optional[List[str]] = None,
        severity: Optional[ReportSeverity] = None,
        only_failing: bool = False,
        sync_security_section: bool = False,
        report_run_id: Optional[str] = None,
    ) -> Dict[str, BenchmarkResult]:
        context = CheckContext(accounts=accounts, severity=severity, only_failed=only_failing)
        config = await self.report_config()
        benchmarks = await self.__benchmarks(benchmark_names)
        # collect all checks
        check_ids = {check for b in benchmarks.values() for check in b.nested_checks() if config.check_allowed(check)}
        checks = await self.list_checks(check_ids=list(check_ids), context=context)
        check_lookup = {check.id: check for check in checks}
        # create benchmark results
        results = await self.__perform_checks(graph, checks, context, config)
        result = {
            name: self.__to_result(benchmark, check_lookup, results, context) for name, benchmark in benchmarks.items()
        }
        if sync_security_section:
            model = await self.model_handler.load_model(graph)
            # In case no run_id is provided, we invent a report run id here.
            run_id = report_run_id or uuid_str()
            await self.db_access.get_graph_db(graph).update_security_section(
                run_id, self.__benchmarks_to_security_iterator(result), model, accounts
            )
        return result

    async def perform_checks(
        self,
        graph: GraphName,
        *,
        provider: Optional[str] = None,
        service: Optional[str] = None,
        category: Optional[str] = None,
        kind: Optional[str] = None,
        check_ids: Optional[List[str]] = None,
        accounts: Optional[List[str]] = None,
        severity: Optional[ReportSeverity] = None,
        only_failing: bool = False,
    ) -> BenchmarkResult:
        context = CheckContext(accounts=accounts, severity=severity, only_failed=only_failing)
        config = await self.report_config()
        checks = await self.list_checks(
            provider=provider,
            service=service,
            category=category,
            kind=kind,
            check_ids=check_ids,
            context=context,
            ignore_checks=config.ignore_checks,
        )
        provider_name = f"{provider}_" if provider else ""
        service_name = f"{service}_" if service else ""
        category_name = f"{category}_" if category else ""
        kind_name = f"{kind}_" if kind else ""
        check_id_name = f"{check_ids[0]}_" if check_ids else ""
        title = f"{provider_name}{service_name}{category_name}{kind_name}{check_id_name}benchmark"
        benchmark = Benchmark(
            id=title,
            title=title,
            description="On demand benchmark",
            documentation="On demand benchmark",
            framework="resoto",
            version="1.0",
            checks=[c.id for c in checks],
            children=[],
        )

        if context.accounts is None:
            context.accounts = await self.__list_accounts(benchmark, graph)

        checks_to_perform = await self.list_checks(check_ids=benchmark.nested_checks(), context=context)
        check_by_id = {c.id: c for c in checks_to_perform}
        results = await self.__perform_checks(graph, checks_to_perform, context, config)
        await self.event_sender.core_event(CoreEvent.BenchmarkPerformed, {"benchmark": benchmark.id})
        return self.__to_result(benchmark, check_by_id, results, context)

    async def filter_checks(self, report_filter: Optional[Callable[[ReportCheck], bool]] = None) -> List[ReportCheck]:
        result = {c.id: c for c in checks_from_file().values() if report_filter is None or report_filter(c)}
        async for c in self.report_check_db.all():
            result.pop(c.id, None)
            if report_filter is None or report_filter(c):
                result[c.id] = c
        return list(result.values())

    async def list_failing_resources(
        self, graph: GraphName, check_uid: str, account_ids: Optional[List[str]] = None
    ) -> AsyncIterator[Json]:
        # create context
        context = CheckContext(accounts=account_ids)
        # get check
        checks = await self.list_checks(check_ids=[check_uid], context=context)
        if not checks:
            raise NotFoundError(f"Check {check_uid} not found")
        model = await self.model_handler.load_model(graph)
        inspection = checks[0]
        # load configuration
        cfg = await self.report_config()
        return await self.__list_failing_resources(graph, model, inspection, cfg, context)

    async def __list_failing_resources(
        self, graph: GraphName, model: Model, inspection: ReportCheck, config: ReportConfig, context: CheckContext
    ) -> AsyncIterator[Json]:
        # final environment: defaults are coming from the check and are eventually overriden in the config
        env = inspection.environment(config.override_values)
        account_id_prop = "ancestors.account.reported.id"

        async def perform_search(search: str) -> AsyncIterator[Json]:
            # parse query
            query = await self.template_expander.parse_query(search, on_section="reported", env=env)
            # filter only relevant accounts if provided
            if context.accounts:
                query = Query.by(P.single(account_id_prop).is_in(context.accounts)).combine(query)
            try:
                async with await self.db_access.get_graph_db(graph).search_list(QueryModel(query, model)) as ctx:
                    async for result in ctx:
                        yield result
            except Exception as e:
                log.warning(f"Error while executing query {query}: {e}. Assume empty result.")

        async def perform_cmd(cmd: str) -> AsyncIterator[Json]:
            # filter only relevant accounts if provided
            if context.accounts:
                account_list = ",".join(f'"{a}"' for a in context.accounts)
                cmd = f"search /{account_id_prop} in [{account_list}] | " + cmd
            cli_result = await self.cli.execute_cli_command(cmd, list_sink, CLIContext(env=env))
            try:
                for result in cli_result[0]:
                    yield result
            except Exception as e:
                log.warning(f"Error while executing command {cmd}: {e}. Assume empty result.")

        async def empty() -> AsyncIterator[Json]:
            if False:  # pylint: disable=using-constant-test
                yield {}  # noqa

        if resoto_search := inspection.detect.get("resoto"):
            return perform_search(resoto_search)
        elif resoto_cmd := inspection.detect.get("resoto_cmd"):
            return perform_cmd(resoto_cmd)
        else:
            return empty()

    def __to_result(
        self,
        benchmark: Benchmark,
        check_by_id: Dict[str, ReportCheck],
        results: Dict[str, SingleCheckResult],
        context: CheckContext,
    ) -> BenchmarkResult:
        def to_result(cc: CheckCollection) -> CheckCollectionResult:
            check_results = []
            for cid in cc.checks or []:
                if (check := check_by_id.get(cid)) is not None:
                    result = results.get(cid, {})
                    count_by_account = {uid: len(failed) for uid, failed in result.items()}
                    check_results.append(CheckResult(check, count_by_account, result))
            children = [to_result(c) for c in cc.children or []]
            return CheckCollectionResult(
                cc.title, cc.description, documentation=cc.documentation, checks=check_results, children=children
            )

        top = to_result(benchmark).filter_result(context.only_failed)
        return BenchmarkResult(
            benchmark.title,
            benchmark.description,
            benchmark.framework,
            benchmark.version,
            documentation=benchmark.documentation,
            checks=top.checks,
            children=top.children,
            accounts=context.accounts,
            only_failed=context.only_failed,
            severity=context.severity,
            id=benchmark.id,
        )

    async def __perform_checks(  # type: ignore
        self, graph: GraphName, checks: List[ReportCheck], context: CheckContext, config: ReportConfig
    ) -> Dict[str, SingleCheckResult]:
        # load model
        model = await self.model_handler.load_model(graph)

        async def perform_single(check: ReportCheck) -> Tuple[str, SingleCheckResult]:
            return check.id, await self.__perform_check(graph, model, check, config, context)

        check_results: Stream[Tuple[str, SingleCheckResult]] = stream.iterate(checks) | pipe.map(
            perform_single, ordered=False, task_limit=context.parallel_checks  # type: ignore
        )
        async with check_results.stream() as streamer:
            return {key: value async for key, value in streamer}

    async def __perform_check(
        self, graph: GraphName, model: Model, inspection: ReportCheck, config: ReportConfig, context: CheckContext
    ) -> SingleCheckResult:
        resources_by_account = defaultdict(list)
        async for resource in await self.__list_failing_resources(graph, model, inspection, config, context):
            account_id = value_in_path(resource, NodePath.ancestor_account_id)
            if account_id:
                resources_by_account[account_id].append(bend(ReportResourceData, resource))
        return resources_by_account

    async def __list_accounts(self, benchmark: Benchmark, graph: GraphName) -> List[str]:
        model = await self.model_handler.load_model(graph)
        gdb = self.db_access.get_graph_db(graph)
        query = Query.by("account")
        if benchmark.clouds:
            query = query.combine(Query.by(P.single("ancestors.cloud.reported.id").is_in(benchmark.clouds)))
        async with await gdb.search_list(QueryModel(query, model)) as crs:
            ids = [value_in_path(a, NodePath.reported_id) async for a in crs]
            return [aid for aid in ids if aid is not None]

    async def validate_benchmark_config(self, cfg_id: ConfigId, json: Json) -> Optional[Json]:
        errors = []
        try:
            benchmark = BenchmarkConfig.from_config(ConfigEntity(ResotoReportBenchmark, json))
            bid = cfg_id.rsplit(".", 1)[-1]
            if benchmark.id != bid:
                errors.append(f"Benchmark id should be {bid} (same as the config name). Got {benchmark.id}")
            errors.extend(await self.__validate_benchmark(benchmark))
        except Exception as e:
            errors.append(f"Can not digest benchmark: {e}")
        return {"errors": errors} if errors else None

    async def validate_check_collection_config(self, json: Json) -> Optional[Json]:
        errors = []
        try:
            for check in ReportCheckCollectionConfig.from_config(ConfigEntity(ResotoReportCheck, json)):
                errors.extend(await self.__validate_check(check))
        except Exception as e:
            errors.append(f"Can not digest check collection: {e}")
        return {"errors": errors} if errors else None

    async def __validate_benchmark(self, benchmark: Benchmark) -> List[str]:
        all_checks = {c.id for c in await self.filter_checks()}
        errors = []
        for check in benchmark.nested_checks():
            if check not in all_checks:
                errors.append(f"Check {check} is defined in the benchmark but does not exist.")
        return errors

    async def __validate_check(self, check: ReportCheck) -> List[str]:
        errors = []
        try:
            env = check.default_values or {}
            detect = ""
            if search := check.detect.get("resoto"):
                detect = search
                await self.template_expander.parse_query(search, on_section="reported", env=env)
            elif cmd := check.detect.get("resoto_cmd"):
                detect = cmd
                await self.cli.evaluate_cli_command(cmd, CLIContext(env=env))
            elif check.detect.get("manual"):
                return []
            else:
                errors.append(f"Check {check.id} neither has a resoto, resoto_cmd or manual defined")
            if not check.result_kinds:
                errors.append(f"Check {check.id} does not define any result kind")
            for rk in check.result_kinds:
                if rk not in detect:
                    errors.append(f"Check {check.id} does not detect result kind {rk}")
            if not check.remediation.text:
                errors.append(f"Check {check.id} does not define any remediation text")
            if not check.remediation.url:
                errors.append(f"Check {check.id} does not define any remediation url")
            for prop in ["id", "title", "risk", "severity"]:
                if not getattr(check, prop, None):
                    errors.append(f"Check {check.id} does not define prop {prop}")
        except Exception as e:
            errors.append(f"Check {check.id} is invalid: {e}")
        return errors

    def __benchmarks_to_security_iterator(
        self, results: Dict[str, BenchmarkResult]
    ) -> AsyncIterator[Tuple[NodeId, List[SecurityIssue]]]:
        # Create a mapping from node_id to all check results that contain this node
        node_result: Dict[str, List[Tuple[BenchmarkResult, CheckResult]]] = defaultdict(list)

        def walk_collection(collection: CheckCollectionResult, parent: BenchmarkResult) -> None:
            for check in collection.checks:
                for resources in check.resources_failing_by_account.values():
                    for resource in resources:
                        node_result[resource["node_id"]].append((parent, check))
            for child in collection.children:
                walk_collection(child, parent)

        for result in results.values():
            walk_collection(result, result)

        async def iterate_nodes() -> AsyncIterator[Tuple[NodeId, List[SecurityIssue]]]:
            for node_id, contexts in node_result.items():
                issues = [
                    SecurityIssue(check=check.check.id, severity=check.check.severity, benchmarks={bench.id})
                    for bench, check in contexts
                ]
                yield NodeId(node_id), issues

        return iterate_nodes()

    @staticmethod
    def on_startup() -> None:
        # make sure benchmarks and checks are loaded
        benchmarks_from_file()
        checks_from_file()


@lru_cache(maxsize=1)
def benchmarks_from_file() -> Dict[str, Benchmark]:
    result = {}
    for name, js in BenchmarkConfig.from_files().items():
        cid = ConfigId(name)
        benchmark = BenchmarkConfig.from_config(ConfigEntity(cid, {BenchmarkConfigRoot: js}))
        result[name] = benchmark
    return result


@lru_cache(maxsize=1)
def checks_from_file() -> Dict[str, ReportCheck]:
    result = {}
    for name, js in ReportCheckCollectionConfig.from_files().items():
        cid = ConfigId(name)
        for check in ReportCheckCollectionConfig.from_config(ConfigEntity(cid, {CheckConfigRoot: js})):
            result[check.id] = check
    return result
