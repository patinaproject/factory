# Upload GitHub demo media

Write the video as a bare `https://github.com/user-attachments/assets/...` URL
in its own paragraph, with a blank line above and below. Without those blank
lines, GitHub renders a raw link instead of a player. Give each still its own
paragraph too. Write it as an `<img>` element carrying `width`, `height`,
`alt`, and `src`. Number the `alt` text and walk the flow in order, such as
`01-email-sent`. Add a caption only where the media does not explain itself.

Create the pull request before you supply the media. The pull request number
is required by the attachment command, and the command updates its body.

Use GitHub CLI version 2.99.0 or newer. Upgrade with your package manager
before continuing when `gh --version` reports an older release.

Use an OAuth, classic PAT, or fine-grained PAT with pull-request write access.
Actions `GITHUB_TOKEN` and GitHub App tokens cannot upload these attachments.
GitHub Enterprise Server is unsupported; use github.com.

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
