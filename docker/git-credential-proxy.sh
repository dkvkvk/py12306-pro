#!/bin/sh
# 仅在需要代理的网络上使用：把 https:// 请求交给本地代理端口。
# 用法（一次性测试推送）：
#   git -c credential.helper= -c http.proxy=http://127.0.0.1:7897 push
true
