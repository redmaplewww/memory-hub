# -*- coding: utf-8 -*-
"""SQLite 并发基元（M8 起全项目共享，消除四处复制）。

- retry_db：BaiduSyncdisk 同步盘瞬时锁库（readonly/locked/busy）线性退避重试；
- LOCK：全局写锁——SessionManager 与 MemoryHub 同库同锁（此前两把锁分治是已知松动点）；
- now：统一时间戳格式。

纪律：连接即用即关（with conn: 只提交不关闭，泄漏句柄会被同步盘锁死库文件）。
"""
import sqlite3
import threading
import time

LOCK = threading.Lock()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def retry_db(fn, tries: int = 5, delay: float = 1.2):
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if ("readonly" in msg or "locked" in msg or "busy" in msg) \
                    and i < tries - 1:
                time.sleep(delay * (i + 1))
                continue
            raise
    return fn()
