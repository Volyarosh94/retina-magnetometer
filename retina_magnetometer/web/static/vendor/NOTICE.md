# Vendored front-end code

## plotly.js basic bundle 3.7.0

- File: `plotly-basic-3.7.0.min.js`, unmodified, from the npm package
  `plotly.js-basic-dist-min@3.7.0` (https://www.npmjs.com/package/plotly.js-basic-dist-min).
- sha256: `c23b03591a6bdad0bd0f47a0c5c52305a5519812b73ce254be65cd841553c9c2`
- Licence: MIT, `plotly-LICENSE.txt` beside it.
- Why vendored: the app runs on a node's LAN, sometimes with no internet at
  all; a chart that loads from a CDN is a blank page there. Why the basic
  bundle: it has the scatter traces this UI uses at a quarter of the full
  bundle's size. Why 3.7.0: the version retina-gui already runs.
- To upgrade: download the new tarball from the npm registry, replace the file,
  update the version in its name, in `templates/index.html` and here, and
  recheck the chart in a browser.
