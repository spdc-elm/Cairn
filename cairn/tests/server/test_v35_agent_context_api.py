from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from cairn.server import db
from cairn.server.app import app
from cairn.server.migrations import runner
from cairn.server.models import CreateProjectRequest
from cairn.server.routers.agent_context import create_template, list_templates, update_template
from cairn.server.routers.projects import create_project, get_project, get_project_agent_context, get_project_graph, list_projects
from cairn.server.models import AgentContextTemplateCreate, AgentContextTemplateUpdate


class AgentContextApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_schema_contains_agent_context_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        runner.migrate(conn)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

        self.assertIn("agent_context_templates", tables)
        self.assertIn("project_agent_contexts", tables)

    def test_template_crud_and_project_snapshot_copy(self) -> None:
        template = create_template(
            AgentContextTemplateCreate(
                name="default",
                description="shared",
                content="base AGENTS",
            )
        )
        self.assertEqual(template.kind, "agents_md")
        self.assertEqual(len(template.content_hash), 64)
        self.assertEqual(list_templates()[0].id, template.id)

        project = create_project(
            CreateProjectRequest(
                title="case",
                origin="start",
                goal="finish",
                agent_context={
                    "template_id": template.id,
                    "content": "project AGENTS",
                    "enabled": True,
                },
            )
        )
        snapshot = get_project_agent_context(project.project.id)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.content, "project AGENTS")
        self.assertEqual(snapshot.source_template_id, template.id)
        self.assertEqual(snapshot.source_template_hash, template.content_hash)

        update_template(template.id, AgentContextTemplateUpdate(content="mutated template"))
        unchanged = get_project_agent_context(project.project.id)
        assert unchanged is not None
        self.assertEqual(unchanged.content, "project AGENTS")

    def test_project_summaries_do_not_leak_full_content(self) -> None:
        create_project(
            CreateProjectRequest(
                title="case",
                origin="start",
                goal="finish",
                agent_context={"content": "SECRET AGENTS BODY", "enabled": True},
            )
        )

        listed = list_projects()[0].model_dump()
        detail = get_project(listed["id"]).project.model_dump()
        graph = get_project_graph(listed["id"])["project"]

        self.assertNotIn("content", listed["agent_context_summary"])
        self.assertNotIn("SECRET AGENTS BODY", str(listed))
        self.assertNotIn("content", detail["agent_context_summary"])
        self.assertNotIn("content", graph["agent_context_summary"])

    def test_agent_context_router_is_registered(self) -> None:
        paths = {route.path for route in app.routes}
        self.assertIn("/agent-context/templates", paths)
        self.assertIn("/projects/{project_id}/agent-context", paths)

    def test_project_settings_ui_preserves_loaded_agent_context_on_save(self) -> None:
        html = Path("cairn/src/cairn/server/static/index.html").read_text(encoding="utf-8")
        self.assertIn("projectAgentContextSnapshot", html)
        self.assertIn("const savedAgentContext = await this.api('PUT'", html)
        self.assertIn("this.projectAgentContextSnapshot = {", html)
        self.assertIn("agent_context_summary: {", html)
        self.assertIn(
            "if (!this.projectSettingsForm.agent_context.loaded && !this.projectSettingsForm.agent_context.content && contextSummary.content_hash)",
            html,
        )


if __name__ == "__main__":
    unittest.main()
