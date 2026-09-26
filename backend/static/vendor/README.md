# Vendored browser dependency

qrcode-generator 2.0.4 by Kazuhiko Arase, MIT.

Source: https://github.com/kazuhikoarase/qrcode-generator/tree/83b7e8fe3fddd3b0368dbafd6ce56995bd25e3c8

`qrcode.js` is the unchanged `js/dist/qrcode.js` browser distribution. Used only to encode payment URLs locally; it makes no network request. The license is retained in `qrcode.LICENSE`. No runtime CDN or external QR endpoint is used.

SHA-256 (`qrcode.js`): `79ec86f82856005b1c887905cfccfcfbec3821ca61c7fd5a952faa5f778f791c`

Source inspection found no fetch, XMLHttpRequest, eval, Function constructor or document.write.
