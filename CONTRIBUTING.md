# Contributing to common-infra-operator

We welcome contributions to this project! Please read the guidelines below before submitting issues or pull requests.

## Developer policies

These policies apply to all forms of activity and engagement in this project.

> [!IMPORTANT]
> AMD employees must also follow the ROCm open source software contributing policies at http://u.amd.com/rocm-oss-policies.

### Licensing

Code contributions to this project are covered under the terms of the [LICENSE](LICENSE) file (Apache 2.0).

All new source files must include the standard AMD copyright header:

```
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
```

### Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<scope>): <short summary>

<body — optional, explain what and why>
```

Common types: `feat`, `fix`, `ci`, `docs`, `chore`, `refactor`, `test`.

### Pull requests

- Target the `main` branch for all contributions.
- Each PR should represent one logical change.
- Include tests for new functionality where applicable.
- Ensure CI passes before requesting review.

### AI tool use policy

We allow the use of AI tools to assist authoring code, issues, pull requests, and reviews. Contributors are fully accountable for all submitted content regardless of how it was authored.

## Getting started

1. Fork the repository and clone your fork.
2. Create a feature branch: `git checkout -b feat/my-change`.
3. Make your changes and commit following the message format above.
4. Push to your fork and open a pull request.

## Contact

For questions, open a [GitHub Discussion](https://github.com/ROCm/common-infra-operator/discussions) or file an issue.
