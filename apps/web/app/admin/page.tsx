import { CreateUserForm, UpdateUserForm } from "@/components/admin/UsersAdmin";
import { CreateRoleForm, UpdateRoleForm } from "@/components/admin/RolesAdmin";
import {
  CreateBrokerGrantForm,
  DeleteBrokerGrantForm,
} from "@/components/admin/BrokerGrantsAdmin";
import LogoutButton from "@/components/LogoutButton";

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
 */
export default function AdminPage() {
  return (
    <main className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Admin</h1>
          <p className="text-sm text-neutral-500">
            Requires the <code>admin:manage</code> permission. If your account
            doesn&apos;t hold it, every action below will return a real 403
            from the backend when you submit it.
          </p>
        </div>
        <LogoutButton />
      </div>

      <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-400">Users</h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <CreateUserForm />
        <UpdateUserForm />
      </div>

      <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-400">Roles</h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <CreateRoleForm />
        <UpdateRoleForm />
      </div>

      <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-400">
        Broker grants
      </h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <CreateBrokerGrantForm />
        <DeleteBrokerGrantForm />
      </div>
    </main>
  );
}
