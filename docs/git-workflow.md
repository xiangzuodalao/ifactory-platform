# Git 工作流

## 仓库模型

`ifactory-platform` 是总控仓，`components/cmms`、`components/thingsboard`、`components/pdm-algorithm`、`components/digital-mcp` 和 `components/platform-integration` 是独立 Git submodule。总仓提交记录每个组件的准确 commit，不配置自动跟随分支。

首次获取工作区：

```bash
git clone --recurse-submodules https://github.com/xiangzuodalao/ifactory-platform.git
cd ifactory-platform
./scripts/bootstrap.sh
./scripts/doctor.sh
```

已有 clone 可执行 `bootstrap.sh` 初始化缺失的 submodule，并仅为 CMMS 和 ThingsBoard 补齐上游 remote。脚本不会切换已初始化组件的分支，也不会安装组件依赖。

## 日常开发

跨平台任务始终从总仓根目录启动 Codex。修改某个组件时，在该组件内创建短生命周期分支：

```text
feat/<ticket>-<topic>
fix/<ticket>-<topic>
chore/<topic>
upgrade/<component>-<version>
```

提交顺序固定为：

1. 在组件仓库中完成修改、测试、提交和推送；
2. 通过组件仓库 PR 合入目标分支；
3. 回到总仓更新该 submodule 指针及必要的契约、文档或平台测试；
4. 在总仓提交并创建 PR。

不要在总仓提交一个脏 submodule，也不要只推送总仓指针而遗漏其指向的组件 commit。进入 submodule 开发前应先创建分支，因为标准 `git submodule update` 通常会让 submodule 处于 detached HEAD。

## 同步开源上游

`bootstrap.sh` 为 CMMS 和 ThingsBoard 配置：

```text
components/cmms          upstream -> https://github.com/grashjs/cmms.git
components/thingsboard   upstream -> https://github.com/thingsboard/thingsboard.git
```

升级流程：

1. `git fetch upstream --tags`；
2. 从当前受支持分支创建 `upgrade/<component>-<version>`；
3. 合并选定的上游稳定 tag；
4. 解决冲突并执行该组件完整测试；
5. 通过组件 PR 合入，再更新总仓指针。

共享升级分支使用 merge 保留上游关系，不对已共享提交 rebase 或 force-push。

## 版本与安全

- 总仓 `main` 与组件默认分支应受保护，禁止 force-push 和删除。
- 发布标签由组件版本与平台组合版本分别管理；平台标签只代表一组已验证的 submodule SHA。
- `.gitmodules` 只允许无凭据 HTTPS URL。
- `.env`、连接串、token、训练数据、checkpoint、数据库配置、构建目录和运行缓存不得提交。
- 若凭据曾出现在 remote URL 或 Git 历史中，仅删除文本不够，必须立即撤销并轮换凭据。
