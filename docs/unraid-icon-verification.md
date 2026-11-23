# Unraid Docker Manager 图标验收

WQN-53 涉及 Docker 页面共用的 `/plugins/dynamix.docker.manager/images/question.png`。旧版移除或更新图标缺失的容器时可能删除这个文件。如果浏览器的图片错误处理器再次请求同一个缺失图标，会产生连续 404。上游修复保留共享文件，并在切换到备用图标前清除错误处理器。Unraid OS 7.3.3 的发行说明列出了此修复。[上游事故](https://github.com/unraid/webgui/issues/2691) · [官方修复](https://github.com/unraid/webgui/pull/2693) · [7.3 回移](https://github.com/unraid/webgui/pull/2708) · [7.3.3 发行说明](https://docs.unraid.net/unraid-os/release-notes/7.3.3/)

## 维护前

在仍有问题时可用只读采样记录基线。打开已登录的 Docker 页，保持平常的容器列表和浏览器负载。若已发生高速刷错，关闭触发页并保留既有日志，避免为了测量再次制造故障。缺少可比较的基线时，错误速率下降这一项保持待验收。

在工作站新建证据目录。以下命令中的 `nas` 代表已授权的 SSH 别名。重定向将结果写到工作站，每次采样使用新的输出文件。采样器读取版本、启动标识、三个文件的摘要，并统计目标时间窗内新追加的 nginx 错误行。日志轮转、截断或窗口新增超过 16 MiB 时，该窗口不可用于验收。采样会保留已有 nginx 日志及 WeatherQuant 数据。

```sh
ssh nas 'bash -s -- 120' < scripts/unraid_icon_sample.sh > before.tsv
```

维护负责人先备份启动设备，核对目标发行说明、兼容性及上一版本恢复条件。如果主机已包含修复，可直接在下一次获准的正常重启前后取证；仍在受影响版本时，使用包含官方修复的受支持发行版，在批准的维护窗口更新和重启。回退使用 Update OS 的上一个版本恢复流程，或按官方指引从维护前启动设备备份恢复。回退到受影响版本后应关闭触发页并保留当次证据。[Unraid 更新与回退](https://docs.unraid.net/unraid-os/updating-unraid/)

## 重启后

1. 在同一 NAS 上再次开始 120 秒采样，保存为 `after.tsv`。工作站和 NAS 需时间同步。
2. 在已登录的 Docker 页等容器列表稳定后，在 DevTools 中运行仓库的 `scripts/unraid_icon_browser.js`。保持标签页可见 61 秒。它向同源图标地址发一次禁用缓存的请求，解码返回图片，然后记录自然发生的后续请求。将控制台打印的 JSON 文本保存为 `browser.json`。报告只包含页面位置、时间、计数及图标内容。
3. 在工作站比较证据，将 `--origin` 换成实际 WebUI 的协议、主机和端口。

```sh
ssh nas 'bash -s -- 120' < scripts/unraid_icon_sample.sh > after.tsv
python scripts/verify_unraid_icon.py --before before.tsv --after after.tsv \
    --browser browser.json --origin http://tower > result.json
```

退出码 0 表示所有条件满足。前后需为同一主机的不同启动，浏览器窗口落在重启后主机采样时段内，PNG 与磁盘图标一致，两个 PHP 文件及图标与已核对的官方版本一致。浏览器窗口内须无额外图标请求、无采样丢失，重启后须无图标错误行，而且 nginx 错误总行数按秒换算后低于基线。额外请求需要检查其发起者，一次额外请求本身不足以断定发生循环。

退出码 1 表示至少一项条件未得到证明，具体项见 `checks`。例如前后错误速率都为零时，可以证明观察期平稳，速率下降仍缺证据。退出码 2 表示输入不完整或不可读。浏览器遇到登录跳转或无法解码图标时会直接报错。主机版本、boot ID、原始采样、浏览器报告及比较结果应作为同一组验收材料保存。

摘要基于官方 7.3.3 标签提交 `31c22f8327cca774dcbddd573dc76f70e87b2f10`，也与该修复的两个合入提交一致。若新版 PHP 已有其他修改，摘要会不同，需要按该发行版逐文件比对两个保护条件并复核对应摘要。仅凭版本号或匿名请求返回 302，无法确认已安装的修复。[7.3.3 源码](https://github.com/unraid/webgui/tree/31c22f8327cca774dcbddd573dc76f70e87b2f10/emhttp/plugins/dynamix.docker.manager)
