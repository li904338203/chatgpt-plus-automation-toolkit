# 面板交付清单（中文）

## 推荐导出命令

```powershell
.\deliver_panel.ps1 -ZipPackage
```

执行后会在下面目录生成干净交付包：

`.\delivery\ChatGPTAssistantPanel_release_YYYYMMDD_HHMMSS`

并同时生成同名 zip 文件。

## 默认导出策略

- 保留运行必需文件：`ChatGPTAssistantPanel.exe`、`_internal`、`data`、`config.yaml`
- 默认移除敏感信息和运行缓存：
  - `.env`
  - `output` 历史
  - `profiles` 历史
  - `logs` 历史

## 常用参数

- 连 `.env` 一起导出：

```powershell
.\deliver_panel.ps1 -ZipPackage -IncludeEnv
```

- 保留历史输出/浏览器配置/日志：

```powershell
.\deliver_panel.ps1 -ZipPackage -IncludeOutput -IncludeProfiles -IncludeLogs
```

## 对方电脑验收步骤

1. 解压到短路径，例如 `D:\ChatGPTAssistantPanel`
2. 右键“以管理员身份运行”一次
3. 运行一次冒烟测试：

```powershell
ChatGPTAssistantPanel.exe --runner paypal-flow1 --count 1 --workers 1 --mail-source hotmail
```

若出现 Playwright 浏览器缺失错误，说明交付包不完整，请重新导出并确认 `_internal\playwright` 存在。

