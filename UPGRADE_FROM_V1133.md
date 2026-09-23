# V1.1.3.3 → V1.2.0

1. 停止当前 Uvicorn。
2. 备份 `data/digital_goods.db`。
3. 用 V1.2.0 文件覆盖项目，但保留原 `.env` 和 `data/digital_goods.db`。
4. 按需将 `.env.example` 中的 `COMMERCIAL_VALIDATION_*` 配置复制到 `.env`；不复制时使用安全默认值。
5. 重新启动 Uvicorn。数据库会自动增加商业验证表和字段，不清空旧数据。
6. 进入“商机中心”，先点击“批量商业验证”。只有 `PASSED` 商机可以继续 AI 分析和商品生成。

默认验证门槛：

- 至少 2 个去重后的真实需求信号；
- 至少 1 个 `explicit_demand`；
- 至少 2 名可识别作者，或来自至少 2 个平台；
- 平均证据质量至少 55；
- 有效发布时间覆盖至少 50%；
- 至少 1 条近 30 天需求；
- 综合验证分至少 65。

验证为本地确定性计算，不调用 Qiniu/SenseNova，不产生 AI 费用，也不会新增任何 evidence。
