import {
  CreateUserForm,
  ResetUserPasswordForm,
  UpdateUserForm,
} from "@/components/admin/UsersAdmin";
import { CreateRoleForm, UpdateRoleForm } from "@/components/admin/RolesAdmin";
import {
  CreateBrokerGrantForm,
  DeleteBrokerGrantForm,
} from "@/components/admin/BrokerGrantsAdmin";
import {
  ChangeBrokerModeForm,
  CreateBrokerForm,
} from "@/components/admin/BrokerModeAdmin";
import AdminGate from "@/components/admin/AdminGate";
import BrokerAccountsAdmin from "@/components/admin/BrokerAccountsAdmin";
import {
  BrokerCatalogue,
  BrokerCredentialsAdmin,
} from "@/components/admin/BrokerBridgeAdmin";
import { EmergencyStopPanel, MarketDataBackfillPanel } from "@/components/admin/SafetyAdmin";
import AppShell from "@/components/shell/AppShell";
import { SectionHeading } from "@/components/ui/primitives";
import {
  UsersList,
  RolesList,
  BrokersList,
  BrokerGrantsList,
} from "@/components/admin/AdminListings";

/**
 * Admin UI for the /admin/* endpoints (docs/API.md, docs/DECISIONS.md D023).
 *
 * This page is reachable by any authenticated user — permission is never
 * guessed client-side. Every form below submits through a route handler
 * that forwards to the real backend, which is the actual `admin:manage`
 * gate; a non-admin user submitting any of these forms sees the backend's
 * real 403 rendered honestly, not a client-side "you can't be here" guess
 * and not a hidden nav item implying a security boundary that doesn't
 * exist at this layer.
 *
 * Phase 45 / D060 gave this page the dashboard's shell and tokens for
 * consistency. No form, endpoint or rendered state changed.
 *
 * Phase 47 added the "Brokers" section: the first frontend for D058's
 * `POST /admin/brokers` and `PATCH /admin/brokers/{id}/mode`, which had
 * been built and tested backend-side with no UI on the grounds that a
 * live-trading surface deserved its own reviewed phase. Both backend
 * endpoints are unchanged; the forms reach them through route handlers
 * exactly like every other form on this page.
 */
export default function AdminPage() {
  return (
    <AppShell
      title="Administration"
      subtitle="Safety switch, market data, users, roles and broker grants."
      statusStrip={<AdminGate />}
    >
      <div className="flex flex-col gap-5">
        <SectionHeading note="read on every order path, never liquidates">
          Safety
        </SectionHeading>
        <EmergencyStopPanel />

        <SectionHeading note="charts, backtests and the bot need stored bars">
          Market data
        </SectionHeading>
        <MarketDataBackfillPanel />

        {/* Phase 90 (D109). Above Users deliberately: wiring a venue is
            the act that decides what this deployment can trade at all,
            and it belongs beside Safety and Market data rather than
            below the account administration. */}
        <SectionHeading note="one adapter and one credential record per venue">
          Broker bridge
        </SectionHeading>
        <BrokerCatalogue />
        <BrokerAccountsAdmin />
        <BrokerCredentialsAdmin />

        <SectionHeading>Users</SectionHeading>
        <div className="grid gap-5 lg:grid-cols-2">
          <CreateUserForm />
          <UpdateUserForm />
        </div>
        {/* Phase 46 / D063. Its own row rather than a third column: on a
            NOT_CONFIGURED deployment this panel renders a live reset link,
            which deserves the full width and an unambiguous warning rather
            than being squeezed beside two unrelated forms. */}
        <ResetUserPasswordForm />
        <UsersList />

        <SectionHeading>Roles</SectionHeading>
        <div className="grid gap-5 lg:grid-cols-2">
          <CreateRoleForm />
          <UpdateRoleForm />
        </div>
        <RolesList />

        <SectionHeading note="kind decides which adapter real orders go to">
          Brokers
        </SectionHeading>
        <div className="grid gap-5 lg:grid-cols-2">
          <CreateBrokerForm />
          <ChangeBrokerModeForm />
        </div>
        <BrokersList />

        <SectionHeading>Broker grants</SectionHeading>
        <div className="grid gap-5 lg:grid-cols-2">
          <CreateBrokerGrantForm />
          <DeleteBrokerGrantForm />
        </div>
        <BrokerGrantsList />
      </div>
    </AppShell>
  );
}
