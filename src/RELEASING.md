# Releasing a version

Use this when a version is final (for example, before migrating a season of data), so
the exact tools can be reproduced later.

1. **Tests pass:** `pytest -q` and `sh test_copy_verify.sh`
2. **Bump the version** in `fits_metadata_extractor.py` AND `fits_migrator.py`
   (`__version__`) AND `copy_verify.sh` (two places: the header comment and the
   `echo` line; both read `fits tools x.y.z`). The tests fail if they differ.
   - New behaviour → bump MINOR (0.8.0 → 0.9.0)
   - Bug fix only   → bump PATCH (0.8.0 → 0.8.1)
   - 1.0.0 = all data migrated and Meridian verified
3. **Update `CHANGELOG.md`** with a new top entry `## [x.y.z] - YYYY-MM-DD`.
   The tests fail if the newest entry is not the current version.
4. **Commit, then tag:**
   ```bash
   git commit -am "Release x.y.z"
   git tag -a vx.y.z -m "fits tools x.y.z"
   git push && git push --tags
   ```
5. **Archive the source** (a frozen copy that never changes):
   ```bash
   mkdir -p ../archive
   git archive --format=tar.gz --prefix=meridian-migration-x.y.z/ vx.y.z \
       -o ../archive/meridian-migration-x.y.z.tar.gz
   ```
6. **Freeze the environment** (so it can be rebuilt on the same platform):
   ```bash
   conda env export --from-history > ../archive/environment-x.y.z.yml
   conda list --explicit        > ../archive/conda-explicit-x.y.z.txt
   pytest -q                    > ../archive/test-results-x.y.z.txt 2>&1
   ```
7. **Note the version** in your processing notes. Every run prints it, and the
   `migration_<time>.log` from an `--execute` run records it with the environment.
