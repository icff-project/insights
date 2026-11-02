"""What an admin may do to a team, and what the Admin team refuses.

`update_team` rewrites a team whole: the name, the roster and the grants all come
from the request, so what the request leaves out is removed. The Admin team is the
exception at both ends — it keeps its name, and it cannot be deleted.
"""

import frappe

from insights.api.user import create_team, delete_team, update_team
from insights.insights.doctype.insights_team.insights_team import clear_cache as clear_team_cache
from insights.tests.base import InsightsIntegrationTestCase
from insights.tests.factories import DT, create_user, delete_users
from insights.tests.permissions_utils import (
    ADMIN,
    USER_1,
    USER_2,
    create_test_users,
)

TEAM_NAME = "Team Test Squad"
RENAMED = "Team Test Renamed Squad"


def members_of(team_name):
    return sorted(member.user for member in frappe.get_doc(DT.TEAM, team_name).team_members)


class TestTeamManagement(InsightsIntegrationTestCase):
    @classmethod
    def before_class(cls):
        create_test_users()

    @classmethod
    def after_class(cls):
        for team_name in (TEAM_NAME, RENAMED):
            if frappe.db.exists(DT.TEAM, team_name):
                frappe.delete_doc(DT.TEAM, team_name, force=True, ignore_permissions=True)
        delete_users(USER_1, USER_2, ADMIN)

    def make_team(self):
        with self.as_user(ADMIN):
            create_team(TEAM_NAME)
        self.addCleanup(self.drop_team)

    def drop_team(self):
        for team_name in (TEAM_NAME, RENAMED):
            if frappe.db.exists(DT.TEAM, team_name):
                frappe.delete_doc(DT.TEAM, team_name, force=True, ignore_permissions=True)

    # @feature settings.team-manage
    def test_an_admin_renames_a_team_and_rewrites_its_members(self):
        self.make_team()

        with self.as_user(ADMIN):
            update_team(
                {
                    "name": TEAM_NAME,
                    "team_name": RENAMED,
                    "team_members": [{"user": USER_1}, {"user": USER_2}],
                    "team_permissions": [],
                }
            )

        self.assertFalse(frappe.db.exists(DT.TEAM, TEAM_NAME))
        self.assertEqual(members_of(RENAMED), sorted([USER_1, USER_2]))

        # the roster is rewritten from the request, so a member left out is removed
        with self.as_user(ADMIN):
            update_team(
                {
                    "name": RENAMED,
                    "team_name": RENAMED,
                    "team_members": [{"user": USER_2}],
                    "team_permissions": [],
                }
            )

        self.assertEqual(members_of(RENAMED), [USER_2])

    # @feature settings.team-manage
    def test_the_admin_team_keeps_its_name_and_cannot_be_deleted(self):
        # the site's Admin team can have rows for users another suite deleted, and
        # a save would fail on the dangling link before it reached the name guard
        admins = [
            member.user
            for member in frappe.get_doc(DT.TEAM, "Admin").team_members
            if frappe.db.exists("User", member.user)
        ]
        self.assertTrue(admins, "the Admin team must hold a member that still exists")

        with self.as_user(ADMIN):
            update_team(
                {
                    "name": "Admin",
                    "team_name": "Renamed Admins",
                    "team_members": [{"user": member} for member in admins],
                    "team_permissions": [],
                }
            )

        self.assertTrue(frappe.db.exists(DT.TEAM, "Admin"))
        self.assertFalse(frappe.db.exists(DT.TEAM, "Renamed Admins"))

        with self.as_user(ADMIN), self.assertRaises(frappe.ValidationError):
            delete_team("Admin")

        self.assertTrue(frappe.db.exists(DT.TEAM, "Admin"))


DELETED_ADMIN = "insights_team_deleted_admin@test.com"
KEPT_ADMIN = "insights_team_kept_admin@test.com"
PROMOTED_USER = "insights_team_promoted_user@test.com"
UNCACHED_MEMBER = "insights_team_uncached_member@test.com"


def team_rows(user):
    return frappe.get_all(
        "Insights Team Member",
        filters={"user": user, "parenttype": DT.TEAM},
        pluck="parent",
    )


class TestAdminTeamFollowsUserRoles(InsightsIntegrationTestCase):
    """`update_admin_team` runs on User `on_change`, which also fires when a user is deleted."""

    def setUp(self):
        self.addCleanup(self.cleanup)
        # a row left by a deletion before this fix fails every save of the Admin team
        dangling = [user for user in members_of("Admin") if not frappe.db.exists("User", user)]
        self.assertEqual(dangling, [], "the Admin team holds rows for deleted users")

    def cleanup(self):
        for user in (DELETED_ADMIN, KEPT_ADMIN, PROMOTED_USER, UNCACHED_MEMBER):
            if frappe.db.exists("User", user):
                frappe.delete_doc("User", user, force=True, ignore_permissions=True)
            frappe.db.delete("Insights Team Member", {"user": user})
        if frappe.db.exists(DT.TEAM, TEAM_NAME):
            frappe.delete_doc(DT.TEAM, TEAM_NAME, force=True, ignore_permissions=True)
        frappe.clear_document_cache(DT.TEAM, "Admin")
        clear_team_cache()

    def test_deleting_an_insights_admin_leaves_no_team_rows_and_keeps_other_members(self):
        create_user(DELETED_ADMIN, roles="Insights Admin")
        create_user(KEPT_ADMIN, roles="Insights Admin")
        team = frappe.get_doc({"doctype": DT.TEAM, "team_name": TEAM_NAME})
        team.append("team_members", {"user": DELETED_ADMIN})
        team.append("team_members", {"user": KEPT_ADMIN})
        team.insert(ignore_permissions=True)
        admins_before = set(members_of("Admin"))
        self.assertIn(DELETED_ADMIN, admins_before)
        self.assertIn(KEPT_ADMIN, admins_before)

        frappe.delete_doc("User", DELETED_ADMIN, force=True, ignore_permissions=True)

        self.assertEqual(team_rows(DELETED_ADMIN), [])
        self.assertEqual(set(members_of("Admin")), admins_before - {DELETED_ADMIN})
        self.assertEqual(members_of(TEAM_NAME), [KEPT_ADMIN])

    def test_a_stale_cached_admin_team_cannot_drop_members_when_a_user_gains_insights_admin(self):
        create_user(PROMOTED_USER, roles="Insights User")
        create_user(UNCACHED_MEMBER, roles="Insights User")
        # the cached copy predates a member another process added
        frappe.get_cached_doc(DT.TEAM, "Admin")
        frappe.get_doc(
            {
                "doctype": "Insights Team Member",
                "parent": "Admin",
                "parenttype": DT.TEAM,
                "parentfield": "team_members",
                "idx": 999,
                "user": UNCACHED_MEMBER,
            }
        ).db_insert()
        clear_team_cache()

        frappe.get_doc("User", PROMOTED_USER).add_roles("Insights Admin")

        admins = members_of("Admin")
        self.assertIn(PROMOTED_USER, admins)
        self.assertIn(UNCACHED_MEMBER, admins)
