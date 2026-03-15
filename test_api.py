import urllib.request
import urllib.error

def test():
    headers = {
        "X-Sc-Nd": "72219953560475168011",
        "X-Requested-With": "XMLHttpRequest",
        "X-Sc-Td": "1773553611",
        "X-Sc-Hb-V": "150",
        "X-Sc-Nt-V": "222",
        "X-Sc-Alias": "neu",
        "X-Sc-Client": "app",
        "X-Sc-Platform": "Android",
        "X-Sc-Device": "00000000-5264-1592-ffff-ffffef05ac4a-7111622f3455a57e",
        "X-Sc-Version": "2.2.2",
        "X-Sc-Token": "T0xyRjhtTGw3eWNnNkxVaWViQ1EyeHdtcXcxRUVTMUgxT0o0bmdyaWxwYz0%3D",
        "X-Sc-Ah": "5BBDB19BC45C5FA72882DAA78D77A18E",
        "User-Agent": "okhttp/4.10.0"
    }
    url = "https://api.app.zanao.com/thread/v2/list?with_reply=true&from_time=0&with_comment=true"
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            print("Status:", response.getcode())
            print("Body:", response.read().decode('utf-8')[:200])
    except urllib.error.URLError as e:
        print("URLError:", e.reason)
    except Exception as e:
        print("Error:", type(e), e)

test()
