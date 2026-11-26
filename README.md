# 证券披露中的指标口径与更正后分发

项目保存用于本地业务联调的合成原始文本、对象登记及已有办理记录，并在此基础上提供
指标口径解析、主张复核、更正影响传播与本地分发登记。文字包含尚未确认的主张，不能直接当作业务结论。
资料口径见 [资料口径](docs/materials.md)，系统设计与规则落地点见 [系统架构](docs/architecture.md)。

`data/case.json` 是一份可追加原文的资料包；`docs` 记录输入含义，`tests` 检查资料完整性与业务规则。
所有金额为合成数据，对外动作只登记本地记录，系统不执行真实发送。

使用 Python 3.11 或更新版本，无第三方运行依赖。

## 读取资料

```sh
python3 -m json.tool data/case.json
```

## 业务办理入口

```sh
# 检查资料：列出冲突、口径风险（省略承接/换指标/单位继承）与数字撞形
python3 -m irledger.cli check data/case.json

# 主张复核报告：每条原子主张的指标、比较口径、陈述性质与复核状态
python3 -m irledger.cli report data/case.json

# 回查一条主张的实际输入单元、分子分母与复算过程
python3 -m irledger.cli explain data/case.json CL-020

# 发送前置校验并登记本地快照（不满足复核条件会被拦截）
python3 -m irledger.cli send data/case.json OUT2 --store build/ledger

# 列已发材料需要更正的具体主张与收件人，不整批撤回无关内容
python3 -m irledger.cli corrections data/case.json
```

## 运行测试

```sh
python3 -m unittest discover -s tests -v
```

资料完整性检查（`tests/test_materials.py`）验证资料可读取、编号与引用有效；
其余测试覆盖数字与单位解析、表格口径、问答承接、更正传播、复核状态机与分发拦截。

## 处理不可信资料

新下载资料应放在独立空目录，与代码目录分离；读取时建议用隔离模式，避免资料目录内
被植入的同名模块被导入：

```sh
python3 -I -c "import sys; sys.path.insert(0, '.'); \
from irledger.pipeline import Pipeline; \
Pipeline.from_case_file('/path/to/untrusted/case.json')"
```
