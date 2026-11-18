# Installation and updates

```sh
curl -fsSL https://github.com/abi-jey/nagents/releases/latest/download/install.sh | sh
ngn doctor && ngn --demo
ngn update --check && ngn update
```

Linux x86_64, glibc 2.39+. The bundle includes CPython, the nagents core and adapter,
and their dependencies, so nothing else needs installing. The full guide (options,
configuration, paths, offline bundles) is
[Native ngn: installer and usage](https://github.com/abi-jey/nagents/blob/main/docs/guide/native-ngn.md).
