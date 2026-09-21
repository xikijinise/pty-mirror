# pty-mirror

这是一个本机 Unix socket 的终端只读监控器。

- `host` 在被监控的终端里启动一个正常的子 shell。你仍然直接操作这个终端。
- 子 shell 的原始输出一份照常显示给你，另一份解析成终端屏幕状态后发给本机 socket。
- `view` 只接收，不读取 stdin，也没有向 `host` 发输入的协议消息。
- host 启动并看到提示符后自动发送一次完整屏幕；你按 Enter 执行命令后，等命令真正回到提示符，再自动发送一次追加更新。
- 默认 Bash 使用不可见的 OSC 提示符标记判断命令完成，所以 `sleep 2; echo done` 不会在 250ms 时被误判完成。

## 一条命令安装

在 Ubuntu 中运行：

```bash
curl -fsSL https://raw.githubusercontent.com/xikijinise/pty-mirror/main/install.sh | bash
```

安装脚本会自动检查 `python3-pyte`、安装程序，并创建 `pm` 命令。重复运行同一条命令即可更新。

不想直接执行网络脚本时，也可以先克隆再安装：

```bash
git clone https://github.com/xikijinise/pty-mirror.git
cd pty-mirror
./install.sh
```

卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/xikijinise/pty-mirror/main/install.sh | bash -s -- --uninstall
```

## 使用数字菜单

安装后只运行一个命令：

```bash
pm
```

然后输入数字选择功能：

```text
pty-mirror
  1) 启动被监控终端
  2) 实时只读查看
  3) 查看当前状态后退出
  4) 输出 JSON 事件
  5) 列出运行中的会话
  6) 显示完整帮助
  0) 退出
```

选择 `1` 后，这个终端仍是你操作的终端。退出子 shell 使用正常的 `exit` 或 `Ctrl-D`。

## 快捷命令

不经过菜单时，原来的快捷命令仍然可用。

如果手动查看接收端，可以运行：

```bash
pm v
```

监控端只会不断打印初始屏幕和每次命令完成后的追加内容。想查看一份当前完整状态并退出：

```bash
pm v --once
```

想让接收端得到机器可读、包含完整屏幕行/路径/提示符的事件：

```bash
pm j
```

## 为什么要重新启动一个 host shell

不能把已经存在的普通终端窗口“无损接管”为可解析屏幕：终端模拟器手里的历史屏幕内容不在 `/dev/pts/N` 里。`host` 自己创建 PTY 后，才能同时把同一份输出显示给你、解析给接收端，并保持当前行编辑和方向键正常工作。

## 范围

普通 Bash 命令行的文字、路径、提示符和 ANSI 控制后的文字网格会被保留，颜色不发送。全屏程序（例如 `vim`、`top`）仍会发送每次完整屏幕状态，但它们不是“追加文本”，接收端应以事件里的 `screen` 为准。
