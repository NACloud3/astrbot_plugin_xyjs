import urllib.request
import hashlib
import time
import random
import json

def test():
    alias = "neu"
    m = "".join(str(random.randint(0, 9)) for _ in range(20))
    td = str(int(time.time()))
    sign = f"{alias}_{m}_{td}_1b6d2514354bc407afdd935f45521a8c"
    ah = hashlib.md5(sign.encode()).hexdigest()
    
    token = "ZjdmVWs3Vm1nWk93dVlPOGsyWFdvb0tNdjkyRnFYbXZoSFdyeWNkMmQ5Vi9sTmlXdm82YjNMZlBwYjZEaU5LcmlhTzBySmkyakplYW5vbVRzcDlxMTMvTXByRzJaNXUzdXJxZ2w1T2RuNldLZk5LYmhwT2J6SWg1Z2RXNW5wVGZsWkt5MjdGK2RhUzJ1S21aaGF1ZmlZK2t5WitEczFpV2VWZHFwUT09"
    
    headers = {
        "X-Sc-Alias": alias,
        "X-Sc-Nd": m,
        "X-Sc-Td": td,
        "X-Sc-Ah": ah,
        "X-Sc-Platform": "windows",
        "X-Sc-Cloud": "0",
        "X-Sc-Appid": "wxa16ce35c0ad1a203",
        "X-Sc-Version": "4.1.2",
        "X-Sc-Od": token,
        "xweb_xhr": "1",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) XWEB/18151",
    }
    
    url = "https://api.x.zanao.com/thread/v2/list?with_reply=true&from_time=0&with_comment=true"
    req = urllib.request.Request(url, data=b"", headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            text = raw.decode("utf-8")
            data = json.loads(text)
            print("errno:", data.get("errno"))
            print("errmsg:", data.get("errmsg"))
            posts = data.get("data", {})
            if isinstance(posts, dict):
                lst = posts.get("list", [])
            else:
                lst = posts
            print(f"Got {len(lst)} posts")
            if lst:
                print("First post title:", lst[0].get("title", "N/A"))
                print("First post content:", lst[0].get("content", "N/A")[:100])
    except Exception as e:
        print("Error:", type(e).__name__, e)

test()
