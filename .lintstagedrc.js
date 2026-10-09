// Lint-staged configuration.
//
// The relative-path conversion is load-bearing, not cosmetic. lint-staged
// passes absolute paths, and markdownlint-cli2 applies its `ignores` only to
// relative ones — so handing the absolute paths straight through would lint the
// vendored skill payloads `.markdownlint-cli2.jsonc` excludes everywhere else,
// which is exactly the pre-commit gap a shared exclusion list exists to close.
//
// No exclusion list is repeated here: converting the paths is what lets the one
// in `.markdownlint-cli2.jsonc` apply.
import { relative } from "node:path";

export default {
  "*.md": (files) => {
    const paths = files
      .map((file) => relative(process.cwd(), file))
      .map((file) => `"${file}"`)
      .join(" ");
    return [`markdownlint-cli2 ${paths}`];
  },
};
