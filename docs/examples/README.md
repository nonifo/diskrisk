# Demo HTML (fictional data)

Open in a browser or screenshot for docs. The UI demo accepts `?view=list|topology|print`.

```bash
cd docs/examples

chromium --headless=new --disable-gpu --no-sandbox --window-size=1440,1200 \
  --screenshot=../images/diskrisk-ui-demo.png \
  "file://$PWD/diskrisk-ui-demo.html?view=list"

chromium --headless=new --disable-gpu --no-sandbox --window-size=1440,1300 \
  --screenshot=../images/diskrisk-topology-demo.png \
  "file://$PWD/diskrisk-ui-demo.html?view=topology"

chromium --headless=new --disable-gpu --no-sandbox --window-size=1440,1200 \
  --screenshot=../images/diskrisk-print-demo.png \
  "file://$PWD/diskrisk-ui-demo.html?view=print"

chromium --headless=new --disable-gpu --no-sandbox --window-size=1440,1100 \
  --screenshot=../images/diskrisk-history-demo.png \
  "file://$PWD/diskrisk-ui-demo.html?view=history"

chromium --headless=new --disable-gpu --no-sandbox --window-size=1400,560 \
  --screenshot=../images/diskinfo-cli-demo.png \
  "file://$PWD/diskinfo-cli-demo.html"
```
