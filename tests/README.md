# Tests

Run the profile/schema smoke tests without manufacturer firmware:

```bash
python3 -m unittest discover -s tests -v
```

The release was also regression-tested locally against the four examined
manufacturer images. Those copyrighted firmware files are intentionally not
included in this repository.
