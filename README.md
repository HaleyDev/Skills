# Skills 仓库

本仓库包含一系列供大模型调用的 skill，大部分 skill 依赖 Python 脚本执行。

## Python 虚拟环境

在执行任何 skill 之前，需要确保当前仓库下存在 Python 虚拟环境：

- 如果 `env/` 目录不存在，先创建虚拟环境：
  ```bash
  python3 -m venv env
  ```
- 如果 `env/` 目录已存在，直接激活使用：
  ```bash
  source env/bin/activate
  ```

激活后再按各 skill 的 `requirements.txt` 安装依赖并运行脚本。