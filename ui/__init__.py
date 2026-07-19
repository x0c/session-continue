"""pickup 的 Textual 界面层：把手写 curses 换成 Textual，后端逻辑不动。

`app.run_app()` 是唯一对外入口，返回值与旧 `curses.wrapper(_run, ...)` 语义一致
（`LaunchRequest | NewSessionRequest | None`），供 pickup.py 的 main()/
_dispatch_direct_launch() 决定是否需要 execvp 接管终端。
"""
