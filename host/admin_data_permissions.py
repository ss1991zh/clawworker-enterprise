"""管理端数据授权业务服务：页面路由只负责收参和返回，规则集中在这里。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse


def is_required_permission_column(column_name: str) -> bool:
    name = (column_name or "").strip().lower()
    return (
        name == "id" or name.endswith("_id") or name.endswith("_no")
        or name.endswith("_code") or name.endswith("编号") or name.endswith("编码")
        or name in {"编号", "编码", "主键"}
    )


def required_permission_columns(catalog_columns) -> list[str]:
    ordered = sorted(catalog_columns, key=lambda item: item.ordinal_position)
    required = [item.column_name for item in ordered
                if is_required_permission_column(item.column_name)]
    return required or ([ordered[0].column_name] if ordered else [])


class DataPermissionAdminService:
    OPERATIONS = frozenset({"browse", "query", "analyze", "export"})

    def __init__(self, *, data_source_store, data_access_store, user_manager,
                 query_gateway=None) -> None:
        self.sources = data_source_store
        self.access = data_access_store
        self.users = user_manager
        self.gateway = query_gateway

    def page_context(self) -> dict[str, Any]:
        sources = self.sources.list_all() if self.sources else []
        source_names = {source.id: source.name for source in sources}
        groups = self.access.list_groups() if self.access else []
        group_subjects = {"@" + group.name: f"group:{group.id}" for group in groups}
        policies = []
        if self.access:
            for policy in self.access.list_all():
                item = policy.__dict__.copy()
                item.update(source_name=source_names.get(policy.data_source_id, "已删除数据源"),
                            subject_kind="user", subject_value=f"user:{policy.username}")
                policies.append(item)
            for policy in self.access.list_group_policies():
                item = policy.__dict__.copy()
                item.update(source_name=source_names.get(policy.data_source_id, "已删除数据源"),
                            subject_kind="group",
                            subject_value=group_subjects.get(policy.username, ""))
                policies.append(item)
        catalog = []
        for source in sources:
            by_table: dict[tuple[str, str], list] = {}
            for column in self.sources.list_catalog(source.id):
                by_table.setdefault((column.schema_name, column.table_name), []).append(column)
            for columns in by_table.values():
                required = set(required_permission_columns(columns))
                for column in columns:
                    catalog.append({"source_id": source.id, **column.__dict__,
                                    "required": column.column_name in required})
        return {
            "policies": policies,
            "sources": sources,
            "source_data": [{"id": source.id, "name": source.name} for source in sources],
            "catalog": catalog,
            "users": sorted(self.users._accounts.keys()),
            "groups": groups,
        }

    @staticmethod
    def _parse_masks(masked_columns: str) -> dict[str, str]:
        masks = {}
        for item in (part.strip() for part in masked_columns.split(",")):
            if not item:
                continue
            if ":" not in item:
                raise ValueError("脱敏配置格式应为 字段:策略，多个用逗号分隔")
            column, strategy = item.split(":", 1)
            masks[column.strip()] = strategy.strip()
        return masks

    def grant_single(self, *, subject: str, data_source_id: str, schema_name: str,
                     table_name: str, allowed_columns: str, operations: list[str],
                     max_rows: int, row_filter_sql: str, masked_columns: str):
        catalog = [column for column in self.sources.list_catalog(data_source_id)
                   if column.schema_name == schema_name and column.table_name == table_name]
        if not catalog:
            raise ValueError("表不在已同步的结构目录中；请先到数据源页面同步结构")
        actual = {column.column_name.lower() for column in catalog}
        columns = [column.strip() for column in allowed_columns.split(",") if column.strip()]
        if "*" not in columns and any(column.lower() not in actual for column in columns):
            raise ValueError("授权字段中包含结构目录不存在的字段")
        if not operations or any(operation not in self.OPERATIONS for operation in operations):
            raise ValueError("请至少选择一种使用权限")
        if self.gateway:
            self.gateway.validate_row_filter(
                data_source_id=data_source_id, row_filter_sql=row_filter_sql,
                catalog_columns=actual,
            )
        common = dict(
            data_source_id=data_source_id, schema_name=schema_name, table_name=table_name,
            allowed_columns=columns, operations=operations, max_rows=max_rows,
            row_filter_sql=row_filter_sql, masked_columns=self._parse_masks(masked_columns),
        )
        if subject.startswith("user:"):
            username = subject.removeprefix("user:")
            if username not in self.users._accounts:
                raise ValueError("用户不存在")
            return self.access.grant(username=username, **common)
        if subject.startswith("group:"):
            return self.access.grant_group(group_id=subject.removeprefix("group:"), **common)
        raise ValueError("授权对象无效")

    def save_batch(self, form) -> tuple[str, int]:
        subject = str(form.get("subject", "")).strip()
        source_id = str(form.get("data_source_id", "")).strip()
        operations = [str(item) for item in form.getlist("operations")]
        max_rows = int(str(form.get("max_rows", "10000")))
        selected_tables = list(dict.fromkeys(
            str(item).strip() for item in form.getlist("tables") if str(item).strip()
        ))
        if not self.sources.get(source_id):
            raise ValueError("数据源不存在")
        if not operations or any(item not in self.OPERATIONS for item in operations):
            raise ValueError("请至少选择一种使用权限")
        if not 1 <= max_rows <= 1_000_000:
            raise ValueError("单次返回行数必须在 1～1,000,000 之间")

        by_table: dict[tuple[str, str], list] = {}
        for column in self.sources.list_catalog(source_id):
            by_table.setdefault((column.schema_name, column.table_name), []).append(column)

        if subject.startswith("user:"):
            username = subject.removeprefix("user:")
            if username not in self.users._accounts:
                raise ValueError("用户不存在")
            grant = lambda **kwargs: self.access.grant(username=username, **kwargs)
            existing = [item for item in self.access.list_all()
                        if item.username == username and item.data_source_id == source_id]
            revoke, subject_name = self.access.revoke, username
        elif subject.startswith("group:"):
            group_id = subject.removeprefix("group:")
            group = self.access.get_group(group_id)
            if not group:
                raise ValueError("用户组不存在")
            grant = lambda **kwargs: self.access.grant_group(group_id=group_id, **kwargs)
            existing = [item for item in self.access.list_group_policies()
                        if item.username == "@" + group.name and item.data_source_id == source_id]
            revoke, subject_name = self.access.revoke_group_policy, group.name
        else:
            raise ValueError("授权对象无效")

        existing_by_table = {(item.schema_name, item.table_name): item for item in existing}
        prepared = []
        for qualified_name in selected_tables:
            if "." not in qualified_name:
                raise ValueError("表标识无效，请刷新页面后重试")
            schema_name, table_name = qualified_name.split(".", 1)
            table_catalog = by_table.get((schema_name, table_name), [])
            if not table_catalog:
                raise ValueError(f"{qualified_name} 不在已同步结构中")
            actual = {item.column_name.lower(): item.column_name for item in table_catalog}
            selected = [str(item).strip() for item in
                        form.getlist(f"columns::{qualified_name}") if str(item).strip()]
            unknown = [item for item in selected if item.lower() not in actual]
            if unknown:
                raise ValueError(f"{qualified_name} 包含不存在的字段")
            columns = list(dict.fromkeys(
                required_permission_columns(table_catalog)
                + [actual[item.lower()] for item in selected]
            ))
            previous = existing_by_table.get((schema_name, table_name))
            prepared.append({
                "data_source_id": source_id, "schema_name": schema_name,
                "table_name": table_name, "allowed_columns": columns,
                "operations": operations, "max_rows": max_rows,
                "row_filter_sql": previous.row_filter_sql if previous else "",
                "masked_columns": ({
                    key: value for key, value in previous.masked_columns.items()
                    if key.lower() in {column.lower() for column in columns}
                } if previous else {}),
            })

        # 所有表先验证完，再整批修改，避免最后一张表错误时留下半套权限。
        for policy_data in prepared:
            grant(**policy_data)
        keep = {(item["schema_name"], item["table_name"]) for item in prepared}
        for old_policy in existing:
            if (old_policy.schema_name, old_policy.table_name) not in keep:
                revoke(old_policy.id)
        return subject_name, len(prepared)


def build_data_permissions_router(
    *, templates, service: DataPermissionAdminService, data_access_store,
    user_manager, flash_redirect, pop_messages,
) -> APIRouter:
    """构造数据权限页面路由；规则和批量保存仍由 service 负责。"""
    router = APIRouter()

    @router.get("/data-permissions", response_class=HTMLResponse)
    def data_permission_list(request: Request):
        return templates.TemplateResponse(
            request, "data_permissions.html",
            {"active": "data_permissions", **service.page_context(),
             "messages": pop_messages(request)},
        )

    @router.post("/data-permissions/groups")
    def data_group_create(request: Request, name: str = Form(...), description: str = Form("")):
        try:
            group = data_access_store.create_group(name, description)
        except ValueError as exc:
            return flash_redirect("/admin/data-permissions", ("error", str(exc)))
        return flash_redirect("/admin/data-permissions", ("success", f"已创建用户组「{group.name}」"))

    @router.post("/data-permissions/groups/{group_id}/delete")
    def data_group_delete(request: Request, group_id: str):
        try:
            data_access_store.delete_group(group_id)
        except ValueError as exc:
            return flash_redirect("/admin/data-permissions", ("error", str(exc)))
        return flash_redirect("/admin/data-permissions", ("success", "用户组及其组权限已删除"))

    @router.post("/data-permissions/groups/{group_id}/members")
    def data_group_member_add(request: Request, group_id: str, username: str = Form(...)):
        if username not in user_manager._accounts:
            return flash_redirect("/admin/data-permissions", ("error", "用户不存在"))
        try:
            data_access_store.add_group_member(group_id, username)
        except ValueError as exc:
            return flash_redirect("/admin/data-permissions", ("error", str(exc)))
        return flash_redirect("/admin/data-permissions", ("success", f"已将「{username}」加入用户组"))

    @router.post("/data-permissions/groups/{group_id}/members/{username}/remove")
    def data_group_member_remove(request: Request, group_id: str, username: str):
        data_access_store.remove_group_member(group_id, username)
        return flash_redirect("/admin/data-permissions", ("success", f"已将「{username}」移出用户组"))

    @router.post("/data-permissions")
    def data_permission_grant(
        request: Request, subject: str = Form(...), data_source_id: str = Form(...),
        schema_name: str = Form(...), table_name: str = Form(...),
        allowed_columns: str = Form(...), operations: list[str] = Form(...),
        max_rows: int = Form(10000), row_filter_sql: str = Form(""),
        masked_columns: str = Form(""),
    ):
        try:
            policy = service.grant_single(
                subject=subject, data_source_id=data_source_id, schema_name=schema_name,
                table_name=table_name, allowed_columns=allowed_columns,
                operations=operations, max_rows=max_rows, row_filter_sql=row_filter_sql,
                masked_columns=masked_columns,
            )
        except ValueError as exc:
            return flash_redirect("/admin/data-permissions", ("error", str(exc)))
        return flash_redirect(
            "/admin/data-permissions",
            ("success", f"已授权「{policy.username}」访问 {policy.schema_name}.{policy.table_name}"),
        )

    @router.post("/data-permissions/batch")
    async def data_permission_batch_grant(request: Request):
        try:
            form = await request.form()
            subject_name, saved_count = service.save_batch(form)
        except (TypeError, ValueError) as exc:
            return flash_redirect("/admin/data-permissions", ("error", str(exc)))
        return flash_redirect(
            "/admin/data-permissions",
            ("success", f"已为「{subject_name}」保存 {saved_count} 张表/视图的访问权限"),
        )

    @router.post("/data-permissions/{policy_id}/revoke")
    def data_permission_revoke(request: Request, policy_id: str):
        try:
            data_access_store.revoke(policy_id)
        except ValueError as exc:
            return flash_redirect("/admin/data-permissions", ("error", str(exc)))
        return flash_redirect("/admin/data-permissions", ("success", "权限已撤销并立即生效"))

    @router.post("/data-permissions/group-policies/{policy_id}/revoke")
    def data_group_permission_revoke(request: Request, policy_id: str):
        try:
            data_access_store.revoke_group_policy(policy_id)
        except ValueError as exc:
            return flash_redirect("/admin/data-permissions", ("error", str(exc)))
        return flash_redirect("/admin/data-permissions", ("success", "组权限已撤销并立即生效"))

    return router
