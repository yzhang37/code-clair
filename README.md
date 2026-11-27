# Clarity: repository-independent code review

This is the upstream source repository for Clarity. Its root contains `engine/`, `initial/`, and this project's own `current/`, without an enclosing `.clarity/` directory. Installation in another project creates a regular `.clarity/` directory inside the chosen host directory.

| Location | Responsibility | During installation or update |
| --- | --- | --- |
| [engine/](engine/) | Review model, the single Principles source, workflow, role prompts, programs, and release metadata | Deployed from the selected upstream Git commit |
| [initial/](initial/) | Factory recovery materials, including rule and case templates | Deployed unchanged from the selected upstream Git commit, alongside `engine/` |
| [current/](current/) | Rules and fixed cases: the project's Registry | Copied from the selected release on first installation; existing host content is preserved |
| [setup.py](setup.py) | Software installation, updates, source records, and Make integration | Deployed so the host can update itself later |
| [Makefile](Makefile) | Upstream offline test entry | Not copied to the host; the installer generates the host's review entries |

The upstream and host layouts relate as follows:

```text
code-clair/                  host-project/
├── .git/                    ├── .git/
├── setup.py                 ├── Makefile
├── README.md                └── .clarity/
├── Makefile                     ├── setup.py
├── engine/                      ├── README.md
├── initial/                     ├── Makefile
└── current/                     ├── engine/
                                ├── initial/
                                └── current/
```

The host can track `.clarity/` in Git; it is not a nested Git repository or submodule. The default **no** tracking choice leaves the index unchanged. Choosing **yes** stages the sidecar and generated Make integration without committing. For installations first made at a Git root, the upstream download cache, installation records, and review runtime state live under `clarity/` in the Git common directory, usually `.git/clarity/`; other installation locations use the local state directory described below.

## Installation and updates

Enter the directory where you want to install Clarity and run:

```sh
curl -fsSL https://raw.githubusercontent.com/yzhang37/code-clair/main/install.sh | sh
```

The installation target is always `.clarity/` in the current directory. That directory can have no Git repository, belong to a mature Git project with business code, history, build files, and uncommitted changes, or be a subdirectory of an existing Git repository. The installer does not move the target to a parent Git root or initialize Git for you. The machine needs working Git, curl, and Python 3.10+. Git downloads the release; its use does not require the target directory to be a Git repository.

A new installation without `--ref` selects the highest stable release from its chosen source, using numeric version comparison. The bootstrap obtains its manager from a fixed formal-release commit. Prereleases and untagged development commits are available through an explicit `--ref`; they are not silently substituted when the source has no stable release. Later updates retain the selected source and selection mode. Existing installations with an older recorded ref, including `HEAD`, keep that ref's meaning rather than being silently switched to stable-release tracking.

The entry follows this sequence:

1. Check whether the current directory already contains a `.clarity` directory, including an installation with missing program files.
2. If present, ask whether to update, the default, or reinstall while keeping existing data.
3. If update is selected, upgrade directly, retain the existing Git tracking choice, and do not ask about tracking again.
4. For a fresh installation outside Git, install the files without managing Git.
5. For a fresh installation inside Git, ask whether to add `.clarity` to Git tracking; the default is **no**.

Installation and reinstallation use the selected committed release for both `engine/` and `initial/`, without generating or rewriting their contents. The source of the host's `current/` depends on what exists before installation:

| Host state | Treatment of `current/` |
| --- | --- |
| No `.clarity/` directory | Copy the selected release's `current/`, including its existing rules and cases |
| `.clarity/current/` already exists | Preserve every existing file and the directory's contents, including an empty or incomplete directory |
| `.clarity/` exists but the entire `current/` directory is missing | Copy the same release's `initial/` verbatim into `current/`; this also applies to updates |

Reinstallation restores missing factory programs and `initial/` while keeping existing `current/`; it asks about Git tracking again. If you manually delete everything in `.clarity/` except `current/`, run the installation command above again to restore the software. If you manually delete the whole `.clarity/` directory, that command performs a first installation with the selected release's `current/`. Missing individual user files are not filled into an existing `current/`, and its content is not merged or repaired automatically.

Recovery copies `initial/` as it is. Its contents may still be templates requiring owner input before review; successful software restoration does not mean that those materials are ready for code review.

Installation and updates generate the host Make entries regardless of the tracking choice. Choosing **no** excludes an untracked sidecar through the local Git `info/exclude`, leaves the index unchanged, and does not change the shared `.gitignore`. Choosing **yes** stages the programs, templates, current materials, and generated Make entries without committing. Existing staging of business files is preserved. Only the added integration is staged for the host Makefile, without including its other working-tree changes. Answering no does not automatically remove already tracked files from Git.

After installation, update directly:

```sh
./.clarity/update
```

Updates replace the factory programs, `initial/`, and any existing runtime snapshot. While `current/` exists, updates and reinstallation retain its contents, model and timeout preferences, and adopted review basis. When the whole `current/` directory has to be restored, old adopted text is retired so it cannot override the restored materials; the next explicit review prepares their basis. Updates do not ask about Git tracking again. Directories without Git can also update.

The first installation defaults to HTTPS from `https://github.com/yzhang37/code-clair.git`. Later updates reuse the selected source and version selection. To synchronize from a local repository, run in the installation directory:

```sh
./.clarity/update --source ../code-clair
```

To switch back to GitHub:

```sh
./.clarity/update --source https://github.com/yzhang37/code-clair.git
```

Maintainers can also run the upstream management program directly from the target directory:

```sh
python3 ../code-clair/setup.py install --source ../code-clair
```

To select an unpublished local commit deliberately, add `--ref HEAD`. Local synchronization reads committed source; it does not distribute the maintainer's uncommitted working-tree changes.

The underlying `setup.py install` and `setup.py update` commands remain available with `--repo`, `--source`, and `--ref`. An explicit `--repo` installs only in that directory, without resolving it to a parent worktree root. Noninteractive installation can use `--track yes` or `--track no` to specify the tracking choice; otherwise it uses the prompt's default. An explicit `--ref` preserves the selected Git tag, branch, or commit; stable tracking retains the intent to follow stable releases rather than pinning the tag chosen by the last installation. The actual full commit is recorded for either selection mode.

## Versions and update checks

Clarity's release version uses [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html): `MAJOR.MINOR.PATCH`. Every core component is a nonnegative decimal integer with no leading zero; `0` is valid, while `00` and `02` are invalid. Components compare numerically, so `4.2.0 < 4.10.0`. A version is not a floating-point number or a lexically sorted string. Formal versions always contain all three core components.

| Release change | Version change |
| --- | --- |
| Backward-compatible correction | `1.2.3` to `1.2.4` |
| Backward-compatible functionality | `1.2.3` to `1.3.0` |
| Incompatible supported usage | `1.2.3` to `2.0.0` |

Compatibility covers supported commands, parameters, exit codes, configuration, and installation/update behavior. A higher major version can require migration; it does not authorize replacing a host's `current/`. A prerelease such as `1.3.0-rc.1` precedes `1.3.0`, and numeric prerelease components also reject leading zeros. Build metadata after `+` never changes version precedence.

The single maintained declaration is [engine/release.json](engine/release.json), deployed unchanged as `.clarity/engine/release.json`. It carries `product_version` and the UTC `built_at` timestamp. The installer obtains the actual full commit from Git; it is not handwritten into a file within that same commit. Installation time is recorded separately as `installed_at`. Both local-source and HTTPS installations of the same release retain the same build timestamp; installing or rebuilding old code later does not make its product version newer.

A formal release requires the matching `v<product_version>` Git tag to point to its exact commit and agree with its metadata. Published tags and release content are immutable. An untagged commit is identified as a development build even if its version declaration still contains the preceding release's number. A package without release metadata is unversioned; a known commit remains useful evidence, but the installer does not invent `0.0.0`, guess a historical release number, or use installation time as build time. The first numbered formal release is `1.0.0`.

Run from an installed host:

```sh
./.clarity/update --version
./.clarity/update --check
./.clarity/update
```

`--version` reads the local installation identity offline, without requiring a Git executable, an Agent, or the ACP Python environment. It reports the release or development/unversioned state, actual commit when recorded, build and installation times, source and selection, and changed or missing factory files. Host `current/` changes are not factory-program modifications.

`--check` retrieves the selected source's candidate and compares it without deployment. It may refresh the download cache; it does not rewrite installed programs, `current/`, model configuration, hooks, the Git index, or the installation record. A failed network operation or insufficient identity evidence is reported as unable to confirm, not as up to date.

Comparison distinguishes a newer candidate, the same release and commit, an older candidate, conflicting identity, development history, and unknown or diverged history. The same formal version number pointing to different commits is an identity conflict. Dates and the alphabetic value of commit hashes do not resolve it. Factory modifications remain visible and retain the existing overwrite protection.

Default update does not silently downgrade to an older candidate. An explicit version selection, for example `./.clarity/update --ref v1.0.0`, requests that exact version, including a deliberate downgrade; it does not reset user materials or preferences. Deploying an old release retains its old identity. A new compatible bug-fix release that withdraws a faulty implementation uses a new patch version; incompatible changes follow the major-version rule. There is no separate system-snapshot or factory-reset subsystem.

Version equality does not skip repair: an update at the same version can still restore missing factory programs or restore an entirely missing `current/` from that release's unchanged `initial/`, under the rules above. Existing modified factory files remain protected. The installation record's existing `version: 1` identifies its record format, not the Clarity product release.

Older installations keep their recorded source, ref, user materials, and preferences. Their old updater does not acquire new query flags until the newer manager is installed; rerunning the installation entry can perform that transition without deleting the sidecar. If an old updater copied new release metadata but did not record tag verification, the product number is shown with an `unverified` release type until a verified update by the new manager records that identity. A known lower product number is still treated as older in this state. Version reporting does not retroactively assign a formal release to an old unversioned package.

## Installation records and protection boundaries

At an existing Git root, Clarity uses `clarity/` in the Git common directory to preserve existing review configuration and records. Outside Git, or when installed in a repository subdirectory, local state lives in `.clarity/.local/`. If the user later creates a Git repository at that directory, its existing local state remains in use, including review configuration and records. The generated `.clarity/.gitignore` excludes local state and Python caches. Download caches are isolated by source, and a failed source switch leaves the selected source unchanged.

Records include the current and previous sources, actual commits, release identity, version-selection intent, deployed-file hashes and permissions, Git tracking choice, and Make entry. Missing factory files can be restored, while locally modified factory files and new paths occupied by local files remain protected against overwrite. When a tracked sidecar is cloned without its local records, updating first matches its remaining factory files against upstream history, checking at most the latest 200 ancestors. The Makefile and ignore file generated by that version are checked too; recognizing an existing directory does not grant ownership of arbitrary local files. Retained local installation records do not require manual cleanup merely because factory files or the whole sidecar were deleted.

Installation and updates provide targets in `.clarity/Makefile` and an include entry in the effective host Makefile, creating a host Makefile when needed. This also applies when tracking is declined or the directory has no Git repository. Existing Makefile content and the default build target are preserved. Conflicting targets owned by others and locally modified managed blocks are not overwritten.

The complete release is prepared before deployment, and a write failure restores the original files. When tracking is chosen, the Git index is also read back and restored within the same transaction. Installation, existing code review, and combined maintenance use the corresponding exclusive locks. Locks and backups left by forced termination or failed rollback require inspection and are not deleted based on age. Cross-file atomic switching during power loss is not guaranteed.

Software installation does not automatically enable commit review or initialize Git. Manual code review can read a plain project directory; Git repositories also retain review of committed and staged snapshots. A new review configuration saves the host's local rules separately from the code under review, as described below; this does not add `.clarity/current/` to Git. Clarity's own activated hook does not keep blocking commits after the whole sidecar has been removed; hooks owned by other tools are left untouched.

## UI language

Interface messages support `en`, `zh-Hans`, `zh-Hant`, `es`, `fr`, `de`, `it`, and `ja`. Selection is automatic from the operating system's preferred UI languages, with English as the fallback. Set `CLARITY_LANG` to override that choice, for example:

```sh
CLARITY_LANG=fr ./.clarity/update
```

[i18n.py](engine/runner/i18n.py) uses the Strategy pattern for OS language detection and a gettext Chain of Responsibility for regional language, base language, then English fallback. Catalogs load on demand. Adding a language requires a JSON catalog in `engine/runner/locales/` and any necessary alias or fallback data, without a class per language. The 10,000-catalog test checks scalability with synthetic data; it does not represent 10,000 translated languages.

Localization applies only to interface labels, help, errors, and progress. Rule text, `initial/`, `current/`, engine documents and prompts remain in their original language. Model inputs, evidence quotations, and original model reasoning in reports are not translated; values inserted into UI messages retain their original text.

## Running reviews in a host

Review requires Python 3.10+ with `venv` and `pip` and an installed, authenticated local Agent. The project directory does not need a Git repository or tracked files. Pi and Codex use the same ACP Python client through their existing ACP adapters. First installation includes the selected release's existing rules and cases. Check that `.clarity/current/rules.md` expresses the standards you want to use, then run from the host directory:

```sh
make review
```

For a new review configuration, Clarity prefers an installed Pi, then Codex. Use `CLARITY_AGENT=pi make review` or `CLARITY_AGENT=codex make review` to choose explicitly on that first review. The choice is saved; later reviews and software updates retain it. Existing `codex_bin` configurations keep their Codex executable preference and now connect through ACP.

The first review that has text to examine prepares the pinned Python dependencies, including ACP 1.0.0 and MCP 1.30.0, in a private user cache. It does not install packages into the host project or global Python. An ACP adapter already on `PATH` is used directly; otherwise Clarity uses `npx` to run its pinned adapter package, so Node/npm must be available for that fallback. Pi or Codex itself must already be installed and authenticated. Model and account availability are determined by that Agent; Clarity does not copy credentials into review reports.

The saved `model` remains optional. Without an explicit model, Pi uses the user's local Pi provider and model settings; Clarity does not maintain a provider catalog. An explicit model is selected through the Agent's advertised ACP model options. Other ACP agents can be selected with `CLARITY_AGENT_COMMAND`, a JSON argument array saved as `agent_command` when preparing a new configuration. The review workflow and result checks are shared; Clarity does not parse each Agent's native CLI output.

The installer copies release materials and prepares Make entries. It does not rewrite the rules for the host or certify their quality. Missing or blank rules are rejected when preparing review, with a message to fill the rules and run again. Rules are not rejected merely because they match `initial/`; their quality and usability are checked by the actual review. Software installation and recovery impose no review-readiness requirement on `initial/`. The unchanged case template is not a formal fixture in a new local-basis configuration. Code review can proceed without fixtures, recording `no_coverage`; this does not establish that the rules passed fixture validation.

On the first `make review` or `make review-staged`, missing configuration and the runtime engine snapshot are prepared automatically, without installing a Git hook. Complete existing configuration is retained while `current/` is preserved. A new configuration, or one rebuilt after restoring the whole `current/`, saves the active local materials as its adopted basis, including when HEAD contains older materials. Later local edits are candidates checked against that saved text; running review again does not adopt them automatically. The saved basis is bound to the project directory, its contents are hashed, and the current local inputs are read again before a result is accepted. Complete existing Git-based configurations retain their Git basis semantics. Restoring an entirely missing `current/` retires the prior adoption, as described above.

Daily host entries:

```sh
make review
make review-staged
make review-rules-quality
make review-fixtures-quality
make review-rules
make review-engine
```

Inside a Git worktree with an existing commit, `review` retains its HEAD scope; `review-staged` examines the index and also works before the first commit. Neither creates a commit. Those Git reviews cover all tracked text in the selected snapshot, including tracked rules, cases, templates, documents, and programs under `.clarity`. Local basis text supplies review criteria separately; untracked or unstaged business code is not added to a Git snapshot. The diff only helps explain changes.

Outside Git, `make review` freezes the current project directory's regular UTF-8 text files directly. A Git repository before its first commit also uses directory input, retaining the Git root scope and common review state even when Clarity is installed in a subdirectory. Directory input respects project `.gitignore` files, excludes `.clarity` and `.git` metadata and an unchanged generated include-only Makefile, and records excluded binary, non-UTF-8, linked, or unsupported paths. The files and exclusions are checked again before accepting a result. There is no Git history from which to infer when a problem was introduced. Outside Git, records and local adopted rules live in `.clarity/.local/`.

When a plain directory has no eligible text, the entry reports that there is nothing to review and exits successfully without calling a model. It does not label an empty input as a passed code review. `review-staged` and commit-hook activation remain Git-specific.

Pi and Codex code-review stages give the Agent a temporary, read-only view of the complete selected text snapshot and the changed or deleted baseline files. Their requests contain the adopted rules and a manifest reference rather than every source file's body. Pi retrieves source with its native `read`, `grep`, `find`, and `ls` tools under an explicit path guard. Its `clarity_read_file` extension tool provides bounded, exact-text continuation when native reading truncates a long line or normalizes a filename. Codex uses three scoped MCP tools: `list_files`, `read_file`, and `search`; its native shell and unified execution are requested disabled. Each independent first review uses a separate session and view. Cross-review and synthesis use the same frozen source. These tool boundaries do not establish universal operating-system confinement, and tool or model limits can still leave a stage incomplete. Custom ACP Agents retain the existing closed, full-inline input path without source-access tools.

The manifest defines the review scope, including tracked files that native search may omit because of ignore settings. Each code stage must retrieve the complete current and changed-baseline text. The engine checks the actual source-delivery record; listing files or receiving search excerpts cannot substitute for full text. Missing ranges leave the review incomplete while preserving validated findings. Same-input retries carry that stage's unresolved findings forward and require an explicit disposition. Complete delivery is necessary for a full review, and does not by itself prove semantic correctness. The MCP reader and Pi's bounded continuation reader also provide original line labels for quotation; those display prefixes are not source text.

A server-side read record alone cannot establish delivery. Code stages also verify completed tool results in the same ACP session against the exact frozen text, original paths and read coordinates. Failed or pending calls receive no source coverage, even when the server already logged a read. Native whole-file results are bound to the recorded session directory. Cached receipts without this transport confirmation require source acquisition again.

If a code stage ends its first turn with missing source text or an invalid JSON/schema response, Clarity can send one bounded feedback message in that same Agent session. It identifies missing original paths and reading coordinates or asks for the required JSON format; the Agent keeps its existing context and tools. Both turns share the configured total timeout. Findings already accepted by the original evidence validator remain recorded and require an explicit disposition, even if continuation fails. The final answer still passes the original evidence, accounting, and complete-delivery checks. Corrupt source-access records cannot be repaired by the model. The protocol record retains both turns and the feedback; a saved initial-prompt hash does not represent the entire conversation.

Health checks, fixture compatibility, blind rules checks, and material-quality checks retain their closed stage inputs; retrieving code does not expose GOLD to blind checks or add criteria from project instructions. ACP progress, tool activity, and the Pi adapter's explicitly declared first startup notification are recorded separately from the final answer, which must still be a schema-valid JSON object. Unexpected prefixes are still rejected. See the [runner's source-access details](engine/runner/README.md#代码阶段的上下文取得).

Capacity limits remain distinct: directory snapshots retain their 1,500,000-byte raw-text limit, while the Codex entry checks its fully assembled request against the known 1,048,576-character input limit. That character limit is not a model token-window size and is not applied to Pi or custom Agents. Custom ACP code reviews retain the legacy 1,500,000-byte serialized input-packet guard. Git input still reports an incomplete review for binary, non-UTF-8, submodule, symbolic-link, or unsupported-mode content. No oversized input is truncated and reported as covered.

To also enable review during Git or SourceTree commits, explicitly run the optional hook activation:

```sh
make review-install
```

It prepares missing review configuration, installs Clarity's commit hook, and preserves existing configuration and adopted basis. It does not run a semantic review and is not a software update command. Use `./.clarity/update` to update the programs.

The two quality checks share one checker and the single Principles source, checking the writing quality of complete rules and complete fixtures respectively. `review-rules` hides GOLD while independently classifying samples, compares fixed expectations, and then checks rationales and scope. `review-engine` combines these three checks and produces only working-tree material-maintenance results; it does not establish a passing code review or rewrite formal materials.

The Reviewer reports review results; an activated Git hook decides whether to permit a commit. After hook activation, normal staging, commit-message entry, and committing in SourceTree trigger the review. To commit despite a review that did not pass, add `Review-Bypass: your one-sentence explanation` on a separate line in the commit message. The hook records all known findings and the actual review status in that commit message before allowing the commit. The explanation needs no AI approval, and bypassing does not change unresolved or incomplete status.

Use `make review-respond RESPONSE='your verbatim response'` for code-review responses and `make review-engine-respond RESPONSE='your verbatim response'` for material-maintenance responses. Responses are saved only in their respective records; they do not automatically change rules or fixed expectations, or generate learn candidates.

## Material and review boundaries

Objects, responsibilities, and terminology are maintained in the [Engine model](engine/README.md). Writing quality for rules and complete fcases uses the same [Principles and teaching comparisons](engine/principles.md). Read them before writing or revising materials, and follow the [workflow](engine/workflow.md) to check material quality and rule compatibility.

`current/rules.md` is a standalone standard for writing and reviewing code. `current/cases/` tests rules using fixed positive and negative expectations, premises, rationales, and scope; it adds no code criteria. One Markdown file manages one case, with additions concerning the same judgment kept in that file. Template headings and unfilled fields are not formal review basis materials; empty templates do not make code pass automatically.

Review checks basis health and compatibility per case before organizing independent code review, counterevidence checks, and a summary. Full capabilities, exit codes, record locations, and limitations are documented in the [runner guide](engine/runner/README.md). Call failures or quotation and coverage gaps cannot be marked as passes. Passing fixed cases does not prove that unknown code has no false positives or missed findings. Automatic knowledge organization is not integrated.

## Upstream verification

Run `make test` in this repository. Offline tests use temporary upstream and host Git repositories, plain project directories, and deterministic ACP agent fixtures to check the review flow, Make integration, installation, and updates. They do not run target business programs or real model reviews. The upstream Makefile does not provide host `review` targets.
