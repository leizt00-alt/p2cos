# social-auto-upload 适配器模板

上游为 https://github.com/dreammis/social-auto-upload ，固定提交 `0012d2c355f88f683cc38dde2a2db209e14091bc`。本适配器使用 `XiaoHongShuNote` 的图片验证和文本填写；浏览器会话、门禁、单次提交和结果核对由本项目控制。

仅在安装缺失时按下列布局恢复；已有目录不要覆盖。使用正常网络代理和系统 CA，保留 TLS 验证。

```bash
mkdir -p /workspace/work/vendor /workspace/work/venvs
git clone https://github.com/dreammis/social-auto-upload.git /workspace/work/vendor/social-auto-upload
git -C /workspace/work/vendor/social-auto-upload checkout --detach 0012d2c355f88f683cc38dde2a2db209e14091bc
cp /workspace/work/vendor/social-auto-upload/conf.example.py /workspace/work/vendor/social-auto-upload/conf.py
python -m venv /workspace/work/venvs/social-auto-upload
/workspace/work/venvs/social-auto-upload/bin/python -m pip install -r /workspace/p2cos/social_auto_upload.requirements.lock
/workspace/work/venvs/social-auto-upload/bin/python -m pip install --no-deps -e /workspace/work/vendor/social-auto-upload
/workspace/work/venvs/social-auto-upload/bin/python /workspace/p2cos/social_auto_upload_adapter.py doctor
```

依赖锁用于该适配器。完整上游 CLI 的平台依赖与行为不在此适配器的验证范围内。实际浏览器控件可能变化，需要在当前网页确认；离线测试通过不证明真实发布成功。

常用入口：

```bash
/workspace/work/venvs/social-auto-upload/bin/python /workspace/p2cos/social_auto_upload_adapter.py check-login
/workspace/work/venvs/social-auto-upload/bin/python /workspace/p2cos/social_auto_upload_adapter.py dry-run --job-id JOB_ID
/workspace/work/venvs/social-auto-upload/bin/python /workspace/p2cos/social_auto_upload_adapter.py publish --job-id JOB_ID
/workspace/work/venvs/social-auto-upload/bin/python /workspace/p2cos/social_auto_upload_adapter.py reconcile --job-id JOB_ID
```

任务命令必须指定持久 `JOB_ID`。先检查该任务状态、完整图片顺序与哈希、独立质量门禁、当前账号、文案、AI 生成说明和原作者/Pixiv出处。不得截断超限图片组。定时发布遵守配置时间，手动立即发布读取数据库内已保存的明确授权。

发布按钮只调用一次。点击后的超时、跳转或审核中状态必须保留 `reconciliation_required`；`reconcile` 只发现公共帖子候选，不自动认定成功。严格核对账号、全部媒体与顺序、文案、发布时间及已发布状态后才记录成功。不能获取真实帖子ID时，仅在全部UI证据已核实时使用专门的UI证据完成方法，帖子ID/URL保持null。

使用旧网页发布方式需要任务授权范围涵盖该回退，并具备主工具明确提交前失败的持久证据。`fallback-preflight --job-id JOB_ID` 返回可回退后，还必须成功执行 `claim-legacy --job-id JOB_ID`。只拿到候选资格、doctor失败或普通网络报错都不能直接上传/发帖。未知、审核中、in_flight和已发表任务不允许回退。旧网页也只允许一次最终提交；结果未知先核实。

第三方源码、venv、浏览器资料目录、登录文件、私有配置和运行证据不进入 Git。
