# GitHub Auth and Permissions

## Quick Start

```bash
export ISSUE_ORCH_GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

This is the simple local mode: issue-orchestrator acts through your GitHub
identity. It is the right first setup for trials, personal repos without strict
approval rules, and teams where someone other than the token owner reviews the
PR.

## Guided Control Center Setup

For a repository discovered by the Control Center, select **Setup**. The GitHub
stage is deliberately resumable:

1. Choose **Use my GitHub identity** or **Use a GitHub App**.
2. Follow the GitHub-side instructions. Setup waits while you create a
   fine-grained token or App, install the App, and save its private key.
3. Return to Setup and select **Verify**.
4. Review the detected identity, repository, credential source, authorship
   tradeoff, and required permissions.
5. Continue to Preview. Setup verifies the same authorization again immediately
   before it writes YAML, prompts, or labels.

Verification is read-only: it proves the selected identity can authenticate and
access the target repository without changing GitHub. It cannot safely prove
every write permission without performing a write, so confirm the listed write
permissions in GitHub. Runtime operations fail clearly if GitHub later denies a
specific operation.

Setup never sends an existing inline YAML token to the browser. A new personal
token is accepted only when you explicitly select **Verify and store token**;
after verification it is stored in a repository-scoped OS keychain entry and
YAML receives only the keychain locator. GitHub App YAML stores a private-key
path or environment-variable name, never the private key. Agents receive
neither form of GitHub credential.

## Authentication Modes

### Simple Mode: Personal Token

Use a fine-grained PAT, GitHub CLI auth, or the OS keychain when you are
comfortable with GitHub API operations and PR creation being attributed to the
token owner. In personal mode, branch pushes continue to use the repository's
configured git transport rather than forcing the PAT into git.

This mode is supported today. It has one important branch-protection limit:
GitHub will not let a PR author approve their own PR. If your repo requires all
PRs to be approved and the orchestrator opens PRs as you, you will need another
eligible reviewer or an admin bypass.

When Setup verifies a credential detected from GitHub CLI, it leaves the YAML
authorization as detected so runtime re-resolves that credential. To pin a
durable, repository-scoped source, use a named environment variable or let
guided Setup store a fine-grained PAT in the OS keychain.

### Protected-Branch Mode: GitHub App

For real protected-branch workflows where you want to approve agent PRs
yourself, use a GitHub App installation identity. The app opens branches and
PRs as `your-app-name[bot]`; you remain the human reviewer, so GitHub does not
treat the approval as self-approval.

This is the recommended target model for bot-authored PRs. First-class GitHub
App configuration is supported through `repo.github.app`. In App mode,
issue-orchestrator uses installation tokens for GitHub REST/GraphQL calls and
for orchestrator-owned `git push` during publish, so created branches and PRs
are authored by the app/bot identity.

In the self-hosted/local model, each user or organization creates its own
GitHub App. Do not share an issue-orchestrator private key with other users. A
single public issue-orchestrator app would only make sense for a hosted service
that securely holds the app private key and mints installation tokens for
installed accounts.

## Creating a Personal Token

### Option 1: Fine-Grained PAT

1. Go to https://github.com/settings/personal-access-tokens/new
2. Select the repository
3. Set permissions:

| Permission | Access |
|------------|--------|
| Contents | Read and write |
| Issues | Read and write |
| Pull requests | Read and write |
| Metadata | Read (automatic) |

### Option 2: Classic PAT

1. Go to https://github.com/settings/tokens/new
2. Select `repo` scope

## Planning a GitHub App

Use a GitHub App when branch protection requires approvals and the normal human
operator should be able to approve agent-created PRs.

Create the app in the account that owns the target repositories, or create an
app that can be installed on any account and install it separately on each
owner account. Each account installation has its own installation ID.

Recommended repository permissions:

| Permission | Access |
|------------|--------|
| Contents | Read and write |
| Issues | Read and write |
| Pull requests | Read and write |
| Metadata | Read (automatic) |
| Checks | Read |
| Commit statuses | Read |

GitHub App UI settings for this use case:

| Setting | Value |
|---------|-------|
| Request user authorization (OAuth) during installation | Off |
| Enable Device Flow | Off |
| Expire user authorization tokens | Leave default; unused |
| Callback URL | Leave blank if allowed, otherwise use the repo URL |
| Webhook Active | Off unless you are building webhook-driven orchestration |

Install with **Only select repositories** and grant access only to repos the
orchestrator should manage. For separate ownership boundaries, such as a
personal repo and an organization repo, prefer separate apps unless you
intentionally want one credential spanning both accounts.

Configure the target repo after installation:

```yaml
repo:
  name: "issue-orchestrator/issue-orchestrator"
  github:
    app:
      client_id: "Iv23..."
      app_id: "4250697"          # optional fallback / metadata
      installation_id: "145305179"
      private_key_path: "~/.config/issue-orchestrator/github-apps/issue-orchestrator-bot.private-key.pem"
```

`client_id` is the preferred JWT issuer. Keep the private key outside the repo
and restrict it to the local operator account, for example:

```bash
mkdir -p ~/.config/issue-orchestrator/github-apps
chmod 700 ~/.config/issue-orchestrator ~/.config/issue-orchestrator/github-apps
chmod 600 ~/.config/issue-orchestrator/github-apps/issue-orchestrator-bot.private-key.pem
```

Alternatively, store the PEM contents in an environment variable and use
`private_key_env` instead of `private_key_path`.

## Rotating an Expiring Token

If GitHub warns that a token is expiring, generate an equivalent token when
GitHub offers that option. Then update the auth source that issue-orchestrator
actually uses:

- exported env var: replace the variable and restart the process
- repo-scoped Keychain entry: replace the configured service/account entry
- global keychain fallback: rerun `issue-orchestrator auth store`
- GitHub CLI auth: refresh the relevant `gh auth` login

Run `issue-orchestrator --config <config-path> doctor` afterward and confirm it
authenticates to the target repo. For the exact resolution order and Keychain
commands, see [GitHub Auth Setup (Developer)](../development/GITHUB_TOKEN_SETUP.md#rotate-an-expiring-token).

## Agent Credentials

**Agents get no GitHub token.** The orchestrator handles all GitHub operations.

See [ADR-0005](../architecture/ADR/0005-human-merge-and-agent-credential-isolation.md) and [ADR-0016](../architecture/ADR/0016-orchestrator-as-mediator.md) for why.

## Token Resolution Details

For the full resolution chain (env var, GitHub CLI `hosts.yml`, keychain) and alternative storage options, see [GitHub Auth Setup (Developer)](../development/GITHUB_TOKEN_SETUP.md).
