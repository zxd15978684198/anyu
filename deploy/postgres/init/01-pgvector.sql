-- 首次初始化数据卷时由 postgres 镜像的 entrypoint 自动执行（数据卷非空时不会重跑）。
--
-- 为什么要这一步：`pgvector/pgvector:pg16` 只把扩展文件装进镜像，**不会**替你在库里
-- CREATE EXTENSION（实测全新库只有 plpgsql）。而迁移 0009 要建 `VECTOR(1536)` 列，
-- 缺扩展就报 `type "vector" does not exist`，表现为 `alembic upgrade head` 直接失败。
--
-- 已经跑过初始化的老数据卷不受影响；本地若遇到该报错，手动执行一次同样的 SQL 即可。
CREATE EXTENSION IF NOT EXISTS vector;
