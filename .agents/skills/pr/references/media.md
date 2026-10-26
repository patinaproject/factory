# Upload GitHub demo media

Write the video as a bare `https://github.com/user-attachments/assets/...` URL
in its own paragraph, with a blank line above and below. Without those blank
lines, GitHub renders a raw link instead of a player. Give each still its own
paragraph too. Write a hand-written still as an `<img>` element carrying
`width`, `height`, `alt`, and `src`. Number the `alt` text and walk the flow in
order, such as `01-email-sent`. Add a caption only where the media does not
explain itself.

## Use a host-provided image uploader

A host-provided image uploader is an upload command or tool that the agent's
host instructions name for pull request images, and that prints `<img>` tags.
The host instructions are the system prompt, `CLAUDE.md` or `AGENTS.md`, or a
provided tool. `github-upload-image` is one example. When the host provides
one, upload raster stills with it and put each `<img>` tag it prints in the
pull request body, in place of `gh pr edit --attach` for those stills. This
path asks for no pull request number and works before the pull request exists.

Use the tag as printed, even without a `height`. Name the files in flow
order, such as `01-email-sent.png`, or edit the printed `alt` value, so the
alt text is numbered. Keep each still in its own paragraph and commit no media
files.

When some files fail, keep the tags the uploader printed and retry only the
failed files. On a persistent failure, report it in the pull request body. Do
not fall back to `gh pr edit --attach` on an App or Actions token.

A project verification skill that saves screenshots, such as
`patinaproject-verify`, sends them through this same step.

This path covers raster stills only. Video and SVG use the attach path below.

## Attach with `gh pr edit`

Create the pull request before you supply the media. The pull request number
is required by the attachment command, and the command updates its body.

Use GitHub CLI version 2.99.0 or newer. Upgrade with your package manager
before continuing when `gh --version` reports an older release.

Use an OAuth, classic PAT, or fine-grained PAT with pull-request write access.
Actions `GITHUB_TOKEN` and GitHub App tokens cannot upload these attachments.
GitHub Enterprise Server is unsupported; use github.com.

An agent on an App or Actions token uses a host-provided image uploader for
stills when one exists. Without one, describe the evidence in the Evidence
text, report that it could not upload, and commit no media.

Keep media outside the repository. Reference each uploaded file locally in
the body first, using `![descriptive alt text](./file)`. Then upload every
file with one command, replacing `<number>` and each path:

```sh
gh pr edit <number> --body-file <body> --attach <image> <video> ...
```

Attach at most 50 files per command. Images must be 10 MB or smaller, and
videos must be 100 MB or smaller on paid plans. Supported types are PNG, JPEG,
GIF, WebP, SVG, MP4, MOV, and WebM. A partial upload still updates the pull
request with successful files and returns non-zero; retry only failed files.

Use concise, meaningful alt text for images by adding `#alt text` to the path,
such as `./login.png#The login error state`. Video attachments have no alt-text
field; describe their visible action and outcome in the surrounding Evidence
text.
Preserve the PR-before-attach order, and commit no media files.
