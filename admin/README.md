# Rankings admin page

`https://admin.redvinesfantasyfootball.com` lets you do the weekly rankings from any browser or phone, without the repo:

1. **Start week N.** This runs the `New week` workflow, which pulls Sleeper data and saves the week as a draft in `weeks/<season>/drafts/week-NN.txt`. The site doesn't show drafts.
2. **Edit.** Drag teams into order, write blurbs, and add pictures and videos. Each upload goes to `media/<team-folder>/`, and its `![](...)` line is added to the blurb for you. The page keeps unsaved edits on your device, and **Save draft** stores them in the repo.
3. **Publish.** This moves the draft to `weeks/<season>/week-NN.txt`. That triggers the `Build site` workflow, and the site updates about a minute later.

The page reads and writes the same `.txt` format as `py ffr.py new`, so you can still edit week files locally. Run `git pull` first.

## How it works

- `src/worker.js` is a Cloudflare Worker that serves `src/admin.html` and calls the GitHub API with a token stored as a Worker secret. It can only write week files and `media/` files, and it can only delete drafts.
- Cloudflare Access handles the login: it emails you a one-time code. The Worker also checks the Access token itself, so if the Access policy is ever removed, the page refuses everyone rather than opening up.
- `.github/workflows/new-week.yml` and `build.yml` do the Python work. `refresh.yml` still runs every morning. All three push through `.github/scripts/push.sh`, which rebuilds and retries when `main` moved during the run.

## One-time setup

You need Node.js installed. Run the commands below from this `admin/` folder.

### 1. GitHub token
1. Go to GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate new token.
2. Under repository access, choose **Only select repositories** and pick `fantasy-football-weekly-review`.
3. Set permissions: **Contents: Read and write** and **Actions: Read and write**. Metadata (read-only) is added automatically.
4. Set an expiration of up to a year, and put a reminder on your calendar to replace it.

### 2. Deploy the Worker
```
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN     # paste the token from step 1
```
`deploy` creates the `admin.redvinesfantasyfootball.com` DNS record itself. The page shows "Not signed in" until step 3 is done. That's expected.

### 3. Cloudflare Access (the login)
1. In the Cloudflare dashboard, open **Zero Trust**. The first visit asks you to pick a team name and the **Free** plan (up to 50 users). Your team domain is `https://<team-name>.cloudflareaccess.com`.
2. Go to **Access → Applications → Add an application → Self-hosted**.
   - Application domain: `admin.redvinesfantasyfootball.com`
   - Policy: action **Allow**, include **Emails** and list the addresses that may sign in.
   - Login method: **One-time PIN** (on by default).
3. Open the application's **Overview** and copy the **Application Audience (AUD) Tag**.
4. In `wrangler.toml`, set `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD`. Optionally, set `ALLOWED_EMAILS` to the same addresses as a second check. Then run `npx wrangler deploy` again.

Open `https://admin.redvinesfantasyfootball.com`, enter the code that arrives by email, and you're in.

## Testing locally
```
npx wrangler dev --var DEV_NO_AUTH:1 --var GITHUB_TOKEN:<token>
```
Then open http://localhost:8787. `DEV_NO_AUTH` only skips the login for requests from your own machine. Reading is safe. **Save**, **Publish**, **Start week** and uploads all change the real repo.

## Limits
- Uploads are capped at about 50 MB per file. Compress big videos and GIFs first; an MP4 is far smaller than the same clip as a GIF.
- Media is committed to the repo, which keeps growing (see `plans/media-on-r2.md`). If media moves to R2 later, only `upload()` in `admin.html` and the `/api/blob` route need to change.
