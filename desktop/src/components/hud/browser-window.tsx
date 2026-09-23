"use client";

import { Globe, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Segmented } from "@/components/hud/options-drawer";
import { TerminalWindow } from "@/components/hud/terminal-window";
import { Select } from "@/components/hud/ui-bits";
import { getBrowser, selectBrowser, type BrowserSelection } from "@/core/computer-use";

/** Which browser and profile a browser task drives: the operator's own, or a fresh one. */
export function BrowserWindow({ onClose }: { onClose: () => void }) {
  const [selection, setSelection] = useState<BrowserSelection | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getBrowser()
      .then(setSelection)
      .catch((error: unknown) => setMessage({ ok: false, text: error instanceof Error ? error.message : "The browser settings could not be read." }));
  }, []);

  const save = (choice: { use_profile: boolean; channel?: string; user_data_dir?: string; profile_directory?: string }) => {
    setSaving(true);
    setMessage(null);
    selectBrowser(choice)
      .then((next) => {
        setSelection(next);
        setMessage({ ok: true, text: "Saved. The next browser task opens it." });
      })
      .catch((error: unknown) => setMessage({ ok: false, text: error instanceof Error ? error.message : "Not saved." }))
      .finally(() => setSaving(false));
  };

  const current = selection?.browsers.find((browser) => browser.user_data_dir === selection.user_data_dir);

  return (
    <TerminalWindow title="browser" icon={<Globe className="size-3.5" />} onClose={onClose} className="h-auto max-h-[84vh]">
      <div className="space-y-3.5 overflow-y-auto p-4">
        {!selection ? (
          <p className="flex items-center gap-2 text-[11px] text-zinc-400">
            <Loader2 className="size-3.5 animate-spin" /> Looking for installed browsers…
          </p>
        ) : (
          <>
            <Field label="Session" hint="Your own profile keeps you signed in to the sites the task needs. A fresh profile is signed in to nothing.">
              <Segmented
                value={selection.use_profile}
                disabled={saving}
                onChange={(use_profile) => save({ use_profile, channel: selection.channel, user_data_dir: selection.user_data_dir, profile_directory: selection.profile_directory })}
                options={[
                  { value: true, label: "My profile" },
                  { value: false, label: "Fresh profile" },
                ]}
              />
            </Field>

            {selection.use_profile && (
              <>
                <Field label="Browser" hint="Only the browsers installed on this machine are listed.">
                  <Select
                    label="Browser"
                    value={current?.user_data_dir ?? ""}
                    options={
                      selection.browsers.length > 0
                        ? selection.browsers.map((browser) => ({ value: browser.user_data_dir, label: browser.label }))
                        : [{ value: "", label: "No Chromium browser found" }]
                    }
                    // A different browser has different profiles: let the gateway pick one in it.
                    onChange={(user_data_dir) =>
                      save({ use_profile: true, channel: selection.browsers.find((browser) => browser.user_data_dir === user_data_dir)?.channel ?? "", user_data_dir, profile_directory: "" })
                    }
                  />
                </Field>

                <Field label="Profile" hint="The profiles that browser shows in its own profile menu.">
                  <Select
                    label="Browser profile"
                    value={selection.profile_directory}
                    options={
                      selection.profiles.length > 0
                        ? selection.profiles.map((profile) => ({ value: profile.directory, label: `${profile.name} (${profile.directory})` }))
                        : [{ value: selection.profile_directory, label: selection.profile_directory || "No profile found" }]
                    }
                    onChange={(profile_directory) => save({ use_profile: true, channel: selection.channel, user_data_dir: selection.user_data_dir, profile_directory })}
                  />
                </Field>

                <p className="break-all rounded-lg border border-white/10 bg-black/30 px-2.5 py-1.5 font-mono text-[10px] text-zinc-500">{selection.user_data_dir || "—"}</p>
                <p className="text-[10px] leading-snug text-amber-300/80">Close that browser before running a task: its profile is locked while it is open.</p>
              </>
            )}

            {message && <p className={message.ok ? "text-[10.5px] text-emerald-300" : "text-[10.5px] text-rose-300"}>{message.text}</p>}
          </>
        )}
      </div>
    </TerminalWindow>
  );
}

function Field({ label, hint, children }: { label: string; hint: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-1 font-mono text-[9px] font-semibold uppercase tracking-[0.2em] text-zinc-500">{label}</p>
      {children}
      <p className="mt-1 text-[9.5px] leading-snug text-zinc-600">{hint}</p>
    </div>
  );
}
