# Release flow

This repository does not publish releases yet. When the first component ships,
releases will use [release-please](https://github.com/googleapis/release-please)
with a single root package:

1. Pull requests merge into the default branch by squash merge, with a
   conventional-commit title.
2. `release-please` keeps one standing release pull request that collects
   `feat:` and `fix:` changes into `CHANGELOG.md` and the next version.
3. Merging the release pull request tags the commit `v<X.Y.Z>` and publishes a
   GitHub Release.

Factories install components from a tagged release, pinned to its commit, so an
update reaches a factory only when its operator moves the pin.

Do not edit released sections of `CHANGELOG.md` by hand.
