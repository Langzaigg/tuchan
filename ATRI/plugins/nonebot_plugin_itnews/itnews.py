# python3
# -*- coding: utf-8 -*-
# @Time    : 2021/11/10 16:59
# @Author  : yzyyz
# @Email   :  youzyyz1384@qq.com
# @File    : itnews.py
# @Software: PyCharm

import requests
from PIL import Image, ImageDraw, ImageFont
import time
import os, textwrap, json

headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWe bKit/537.36 (KHTML, like Gecko) Chrome/93.0.4544.0 Safari/537.36 Edg/93.0.933.1",
}

width = 18

def get_news(keys):
    year = time.strftime("%Y", time.localtime())
    mon = time.strftime("%m", time.localtime())
    day = time.strftime("%d", time.localtime())
    jname = "./data/news/"+str(year) + str(mon) + str(day) + ".json"
    if os.path.exists(jname)==False:
        data_dic = {'newslist':[]}
        ctime_list = [time.strftime("%Y-%m-%d", time.localtime()), time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))]
        api_dic = {'ai': 10, 'keji': 10, 'internet': 5, 'dongman': 3, 'esports': 5, 'it': 5, 'generalnews': 20}
        ids = 0
        html = {}
        for api, maxn in api_dic.items():
            mids = ids + maxn
            url = f"http://api.tianapi.com/{api}/index?key={keys}&num=50"
            html = requests.get(url,headers=headers).json()
            print(api)
            for title in html["newslist"]:
                if ids >= mids or ids >= 20:
                    break
                if title['ctime'].split()[0] in ctime_list and len(title['description']) > width and (title not in data_dic['newslist']):
                    print(title["title"])
                    data_dic['newslist'].append(title)
                    ids += 1
        if ids < 20:
            for title in html["newslist"]:
                if ids >= 20:
                    break
                if title not in data_dic['newslist']:
                    print(title["title"])
                    data_dic['newslist'].append(title)
                    ids += 1
        with open(jname, 'w', encoding="utf-8") as f:
            json.dump(data_dic, f)
            print('===Dumped===')
        
    else:
        with open(jname, 'r', encoding="utf-8") as f:
            data_dic = json.load(f)
        
    data = []
    ddata = []
    pdata = []
    udata = []
    for title in data_dic["newslist"]:
        print(title["title"])
        data.append(title["title"])
        ddata.append(title["description"])
        pdata.append(title["picUrl"])
        udata.append(title["url"])
    return data, ddata, pdata, udata


def draw_news(keys, ids):
    year = time.strftime("%Y", time.localtime())
    mon = time.strftime("%m", time.localtime())
    day = time.strftime("%d", time.localtime())
    fname = "./data/news/"+str(year) + str(mon) + str(day) + ".png"
    datetitle = str(year) + "." + str(mon) + "." + str(day)
    bgpath = "./data/source"
    bgname = "./data/source/background.png"
    ttfname = "./data/source/font.ttc"
    newspath = "./data/news"
    if os.path.exists(bgname)==False:
        print("创建资源目录")
        os.mkdir(bgpath)
    if os.path.exists(bgname)==False:
        print("下载资源图片")
        bg=requests.get("https://cdn.mengze.vip/gh/yzyyz1387/blogimages/background.png").content
        with open(bgname,"wb") as fp:
            fp.write(bg)
            fp.close()
    if os.path.exists(ttfname)==False:
        print("下载资源字体")
        tf = requests.get("https://cdn.mengze.vip/gh/yzyyz1387/blogimages/font.ttc").content
        with open(ttfname,"wb") as tfn:
            tfn.write(tf)
            tfn.close()
    if os.path.exists(newspath)==False:
        print("创建输出目录")
        os.mkdir(newspath)
    news_list, des_list, pic_list, url_list = get_news(keys)
    text = "兔酱NEWS"
    ttfpath = "./data/source/font.ttc"
    bgpath = "./data/source/background.png"
    chars_x = 50
    img = Image.open(bgpath)
    ttf = ImageFont.truetype(ttfpath, 30)
    tttf = ImageFont.truetype(ttfpath, 50)
    tf = ImageFont.truetype(ttfpath, 160)
    img_draw = ImageDraw.Draw(img)
    img_draw.text((100, 140), text, font=tf, fill=(255, 255, 255))
    img_draw.text((400, 400), datetitle, font=ttf, fill=(255, 255, 255))
    chars_y = 500
    if ids:
        fname = "./data/news/"+str(year) + str(mon) + str(day) + '_' + str(ids) + ".png" 
        if len(news_list[ids-1]) > width:
            tlines = textwrap.wrap(news_list[ids-1], width=width)
            for i in tlines:
                img_draw.text((chars_x, chars_y), i, font=tttf, fill=(0, 0, 0))
                print(i)
                chars_y += 80
            chars_y += 20
        else:
            img_draw.text((chars_x, chars_y), news_list[ids-1], font=tttf, fill=(0, 0, 0))
            print(news_list[ids-1])
            chars_y += 75
        lines = textwrap.wrap(des_list[ids-1], width=width)
        for i in lines:
            img_draw.text((chars_x, chars_y), i, font=tttf, fill=(100, 100, 100))
            chars_y += 70
        byttf = ImageFont.truetype(ttfpath, 15)
        img_draw.text((850, 1550), "", font=byttf, fill=(0, 0, 0))
        img.save(fname)
    else:
        fname = "./data/news/"+str(year) + str(mon) + str(day) + ".png"
        for i in range(min(20, len(news_list))):
            img_draw.text((chars_x, chars_y), str(i + 1) + ". " + news_list[i], font=ttf, fill=(100, 100, 100))
            chars_y += 55
        byttf = ImageFont.truetype(ttfpath, 15)
        img_draw.text((850, 1550), "", font=byttf, fill=(0, 0, 0))
        img.save(fname)
