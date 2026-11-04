# 五篇通告输入目录

将五篇 UTF-8 编码的交通管制通告放在此目录，每篇保存为一个文件。文件可以使用
`.txt` 扩展名，也可以使用 `notice04` 这样的无扩展名名称。

文件名必须不同，建议使用：

- `notice_01.txt`
- `notice_02.txt`
- `notice_03.txt`
- `notice_04.txt`
- `notice_05.txt`

准备完成后，在项目目录运行：

```powershell
.\run_five_notices.ps1
```

结果及每篇通告的运行日志会写入 `output` 目录。

脚本会按文件名顺序逐篇处理全部五篇，每次只向本机 Ollama 提交一篇；单篇失败时
自动重试一次，完成后再处理下一篇。
