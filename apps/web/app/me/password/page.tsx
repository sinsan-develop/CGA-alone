"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";

import { apiRequest } from "@/lib/api";
import { loadAuthSession } from "@/lib/auth";
import { useI18n } from "@/components/language-provider";
import { ACCOUNT_PAGE_CATALOGS } from "@/lib/i18n/account-pages";

export default function PasswordPage() {
  const router = useRouter();
  const { language } = useI18n();
  const copy = ACCOUNT_PAGE_CATALOGS[language].password;
  const [token, setToken] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newPasswordConfirm, setNewPasswordConfirm] = useState("");
  const [message, setMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const session = loadAuthSession();
    if (!session) {
      setErrorMessage(copy.loginRequired);
      return;
    }
    setToken(session.access_token);
  }, [copy.loginRequired]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage("");
    setErrorMessage("");

    if (newPassword !== newPasswordConfirm) {
      setErrorMessage(copy.mismatch);
      return;
    }

    setSubmitting(true);
    try {
      const result = await apiRequest<{ message: string }>(
        "/api/v1/auth/change-password",
        {
          method: "POST",
          body: JSON.stringify({
            current_password: currentPassword,
            new_password: newPassword,
            new_password_confirm: newPasswordConfirm,
          }),
        },
        token,
      );
      setMessage(result.message);
      setCurrentPassword("");
      setNewPassword("");
      setNewPasswordConfirm("");
      router.replace("/studio/workspace");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : copy.changeFailed);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="page">
      <section className="auth-form-card">
        <div className="admin-detail__header">
          <div>
            <h2>{copy.title}</h2>
            <p>{copy.description}</p>
          </div>
          <Link href="/me/profile" className="secondary-action">
            {copy.profile}
          </Link>
        </div>

        <form className="auth-form-card__body" onSubmit={handleSubmit}>
          <label className="field-block">
            <span>{copy.currentPassword}</span>
            <input
              className="input-control"
              type="password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>

          <label className="field-block">
            <span>{copy.newPassword}</span>
            <input
              className="input-control"
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              autoComplete="new-password"
            />
          </label>

          <label className="field-block">
            <span>{copy.newPasswordConfirm}</span>
            <input
              className="input-control"
              type="password"
              value={newPasswordConfirm}
              onChange={(event) => setNewPasswordConfirm(event.target.value)}
              autoComplete="new-password"
            />
          </label>

          {message ? <p className="form-message form-message--success">{message}</p> : null}
          {errorMessage ? <p className="form-message form-message--error">{errorMessage}</p> : null}

          <button
            type="submit"
            className="primary-action primary-action--full"
            disabled={
              submitting ||
              !token ||
              !currentPassword ||
              !newPassword ||
              !newPasswordConfirm
            }
          >
            {submitting ? copy.changing : copy.submit}
          </button>
        </form>
      </section>
    </main>
  );
}
