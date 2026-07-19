"""pickup 的 Textual 界面层：唯一入口是左右分栏主屏。

`app.run_app()` 是唯一对外入口，返回
`LaunchRequest | NewSessionRequest | None`，供 pickup.py 的 main()/
_dispatch_direct_launch() 决定是否需要 execvp 接管终端。
"""
