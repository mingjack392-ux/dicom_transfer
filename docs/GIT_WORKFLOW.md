# 本地 Git 归档与日常提交

本地归档分支 `codex/project-baseline-20260922` 保存已实现的源码、合成测试、依赖清单和
通用文档。远程发布分支 `codex/safe-baseline-20260923` 使用当前文件树创建独立根提交，
使旧初始化提交不进入新分支的历史。原有 `main` 分支及其历史保留。

当前 GitHub 默认分支和日常维护基线为 `codex/safe-baseline-20260923`，已合入
`manage-dicom-data` skill。后续功能分支从该分支创建；旧 `main` 留作历史参考。

## 提交范围

纳入：`src/` 下维护中的业务入口和内部模块、`tests/test_*.py`、根目录依赖文件、
经审查的通用文档、混合包转存的三个 Python 文件和其 requirements、通用 Linux
复制/启动脚本及合成性能测试脚本，以及 `skills/manage-dicom-data/` 下可安装的通用
skill 和参考资料。该 skill 以仓库版本为维护源，更新后同步到本机安装目录。

skill 中的项目路径相对 checkout；流程指引随 skill 一起提交，避免引用仅存在于本地的
中心操作指南。新增 skill 内容也需检查是否含真实病例、部署地址或凭据；映射和运行产物
继续由既有规则忽略。

`.gitignore` 对混有业务数据的 `web_registry/`、部署脚本 `scripts/` 和 `docs/`
采用明确白名单。新增通用文件先审查，再添加放行规则。

以下内容保留原位、由 Git 忽略，不属于仓库备份范围：

- DICOM、Excel、CSV、映射、身份状态、断点、日志、真实中心数据目录和输出产物；
- `release/` 中的 ZIP 及校验文件、`artifacts/`、临时目录和环境依赖；
- 含实际病例 UID、中心病例记录或内部部署路径的操作指南；
- 旧 MySQL 入口及其本地连接配置、旧启动器、一次性人工匹配脚本；
- 具体环境的备份/删除/监控脚本，以及依赖本地案例指南的历史打包脚本。

Git 忽略不会删除任何文件，也不会备份这些文件。既有发布 ZIP 及对应校验值保持原样；
源码归档不宣称能从公开依赖独立重建所有历史中心发布包。

## 常用操作

查看工作区与近期记录：

```bash
git status --short
git log -5 --oneline
git diff
```

对本次修改的确切文件暂存，审查暂存内容再提交，例如：

```bash
git add -- src/scan_dicom_patients.py tests/test_scan_dicom_patients.py
git diff --cached --stat
git diff --cached
git diff --cached --check
git commit -m "fix: describe the verified change"
```

检查某文件为何被忽略：

```bash
git check-ignore -v -- path/to/file
```

新增功能前可以建立新分支，分支名使用 `codex/` 前缀。对已审核代码形成提交即可，
无需为了清空 `git status` 删除本地业务文件。

## 发布边界

推送前审查拟公开文件与提交历史是否包含真实病例样例或内部信息。初始化提交中已有的
文本不会因当前文件替换样例而从旧分支历史自动消失。本次独立发布分支不继承该历史，
也未重写已有 `main` 或改变远程仓库可见性。

自动化测试结果及运行依赖记录在 [项目功能基线](PROJECT_BASELINE.md)。
