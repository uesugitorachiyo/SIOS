# Third-party dependencies

SIOS carries forward AIOS-Public's MIT-licensed source. This source repository
does not vendor third-party packages or their runtime data. A Python 3.12
resolution is version-listed in `requirements.basic-memory.lock`;
its full license inventory has not yet been reviewed for redistribution. The
local test environment is not part of this Git tree.

SIOS source is licensed under the repository's [MIT license](LICENSE). Its
Python dependencies have their own licenses. Installing SIOS through `pip` or
`uv` obtains those packages separately.

An inherited AIOS-Public license inventory remains in
[`docs/DEPENDENCY_LICENSES.md`](docs/DEPENDENCY_LICENSES.md) for provenance.
It describes the older AIOS-Public lock and must not be treated as the current
SIOS dependency inventory.

When redistributing dependencies or a combined application, review the exact
distributions and carry their applicable licenses and notices.

[Basic Memory 0.23.2](https://pypi.org/project/basic-memory/0.23.2/) is an optional, external SIOS index dependency. It is installed only in the ignored SIOS virtual environment, not copied into this repository. Its release metadata declares `AGPL-3.0-or-later`; any redistribution of a combined application needs its separate license review.
