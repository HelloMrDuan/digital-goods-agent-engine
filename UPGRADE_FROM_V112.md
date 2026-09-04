# 从 V1.1.2 升级到 V1.1.3

建议先停止当前 Uvicorn，再覆盖补丁文件。

## 必须保留

不要覆盖/删除你现有的：

```text
.env
data/digital_goods.db
```

V1.1.3 会在启动时自动给旧数据库补充意图/评论字段，并对历史真实 evidence 执行本地意图回填。

## 启动

```powershell
python scripts/doctor.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## OpenCLI / P0

Chrome + Browser Bridge 保持可连接，小红书/Reddit 登录态仍有效即可；目标页面无需常驻。

建议先在 PowerShell 验证：

```powershell
opencli doctor
opencli xiaohongshu search "Excel模板" -f yaml
opencli reddit search "excel template" -f yaml
```

小红书评论采集由系统使用搜索结果中的完整签名 URL 自动执行，不要手工只传 note id。

## 推荐操作

升级后进入采集中心：

1. 点击“同步赚钱优先推荐源”；
2. 确认 P0 小红书/Reddit 的 enrich 配置已开启；
3. 执行“采集核心需求”；
4. 在真实证据库查看 `intent_class`；
5. 在商机中心确认只有 explicit/probable demand 进入需求簇。
