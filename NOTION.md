# Notion Setup Guide

One-time configuration for the Notion integration, workspace structure, and webhook.

---

## 1. Create an integration

Go to [notion.so/profile/integrations](https://notion.so/profile/integrations/internal) → **Create a new integration**. 

Select a workspace of your choice to save to → click Create → Configure integration settings. 

| Field | Value |
|---|---|
| Name | Rosetta (or whatever you like) |
| Capabilities | Read content, Update content, Insert content |

Click **Save**. Take note of the **Internal Integration Secret** (`ntn_...`) — this is your **Notion integration token**.

---

## 2. Create the workspace structure

You have two options:

### Option A — let `rosetta setup` do it (recommended)

Run `rosetta setup` and choose **Create it for me** at the workspace step. It will:

1. Ask you to create a page and share it with your integration (instructions below)
2. Create **New Hire Requests** database inside that page
3. Create **Wiki Archive** page inside that page (with a yellow archive icon)

### Option B — manually

Create a top-level page in Notion (your onboarding hub). Inside it, create a database called **New Hire Requests** with this schema:

| Property | Type | Notes |
|---|---|---|
| Name | Title | New hire's full name |
| Role | Text | e.g. "Backend Engineer" |
| GitHub Repos | Text | One URL per line |
| Notes | Text | Extra context for the agent |
| Status | Select | Options: Pending, Ready, Processing, Done |
| Wiki URL | URL | Written back by Rosetta after wiki generation |
| Contact Email | Email | Used for email notifications (optional) |
| Slack Handle | Text | Used for Slack DM — with or without `@` (optional) |

Also create a **Wiki Archive** page alongside the database — old wikis are moved here on full refresh.

---

## 3. Share the hub page with your integration

Notion's internal integrations can only access pages they've been explicitly connected to.

1. Open your onboarding hub page in Notion
2. Click **...** (top right) → **Connect to**
3. Select your integration from the list

The database and all child pages (generated wikis) are automatically included — you only need to do this once on the top-level page.

---

## 4. Make the hub page public

New hires receive a link to their wiki via Slack. If they don't have a Notion account, the page needs to be publicly accessible.

1. Open the onboarding hub page
2. Click **Share** (top right) → **Share to web**
3. Set to **Anyone with the link can view**

Child wiki pages inherit this setting automatically.

---

## 5. Copy the page IDs

The page ID is the 32-character hex string at the end of any Notion URL:

```
https://www.notion.so/My-Page-Title-a1b2c3d4e5f6...
                                     ^^^^^^^^^^^^^^^^ this part
```

Set these in your `.env` (or run `rosetta setup` which does this automatically):

```
NOTION_ONBOARDING_PAGE_ID=<hub page ID>
NOTION_DATABASE_ID=<New Hire Requests database ID>
NOTION_GRAVEYARD_PAGE_ID=<Wiki Archive page ID>
```

---

## 6. Webhook setup (optional)

Without a webhook, Rosetta polls the database every 5 minutes for `Status = Ready` rows. The webhook makes triggering instant — the moment a team lead sets a row to Ready, Rosetta starts generating.

### Prerequisites

- `rosetta serve` must be running
- A public URL pointing to your server (ngrok for local dev)

### Step-by-step

**1. Start ngrok**

```bash
ngrok http 8000
```

Copy the `https://` Forwarding URL. Add it to your `.env`:

```
WEBHOOK_PUBLIC_URL=https://abc123.ngrok-free.app
```

> ngrok free tier generates a new URL every session. When you restart ngrok, update `WEBHOOK_PUBLIC_URL` in `.env` and the webhook URL in the Notion dashboard, then restart `rosetta serve`.

**2. Start Rosetta**

```bash
rosetta serve
```

**3. Register the webhook in Notion**

Go to [notion.so/profile/integrations](https://notion.so/profile/integrations) → your integration → **Webhooks** tab → **Add webhook**.

| Field | Value |
|---|---|
| URL | `https://<your-ngrok-url>/webhook/notion` |
| Events | `page.properties_updated` only — uncheck everything else |

Click **Create subscription**.

**4. Verify the webhook**

Notion immediately POSTs a verification token to your endpoint. Rosetta prints it in the terminal and writes it to `.env` automatically:

```
============================================================
  Notion webhook verification token received:
  abc123...
  Paste this into the Notion dashboard to activate the webhook.
============================================================

  NOTION_WEBHOOK_SECRET written to .env and active.
```

Copy the token, paste it into the Notion verification form, and click **Verify**. The subscription is now active — no server restart needed.

**5. Confirm in `rosetta doctor`**

```bash
rosetta doctor
```

The **Notion webhook** row should show `✔  enabled  (https://…/webhook/notion)`.

---

## How the integration is used

| Action | API calls made |
|---|---|
| `rosetta setup` | Creates database + archive page inside your hub |
| `rosetta serve` (poll) | Queries database every 5 min for `Status = Ready` rows |
| `rosetta serve` (webhook) | Receives `page.properties_updated` events from Notion |
| `rosetta onboard` | Reads the DB row, creates a wiki page as a child of the hub |
| Wiki generation | Writes sections to the new wiki page, updates `Wiki URL` + `Status` on the DB row |
| `rosetta refresh` | Archives old wiki pages to Wiki Archive, creates new ones |
