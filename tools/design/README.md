# 设计生成与验证工具

这些脚本只生成/验证设计，不是应用实现，不会连接业务DB或调用真实模型。

## 文件

- build_specs.py：公共Schema、API与契约生成入口。
- extra_specs.py：AI、规则、模型、任务与事件契约。
- release_specs.py：模型模式、校准、验收policy、评测和批准台账契约。
- release_reference.py / test_release_design.py：发布门禁纯参考函数与模拟测试，不是真实模型评测或人工批准。
- data_model.py：数据字典和MySQL设计DDL。
- rule_reference.py：rules-dnf-v1参考语义；无真实安全知识。
- readiness_reference.py / test_readiness_design.py：IRR六项关闭参考及合成边界；覆盖状态循环、三值、容量、对象操作矩阵、公共端点配置、质量标量/关系/OCR词法。不执行真实IAM、签名或图片库。
- validate_specs.py：OpenAPI/JSON Schema、正反例、FK、规则、文档链接验证。
- requirements.txt：设计验证依赖，不是应用运行依赖。

## 使用

~~~powershell
python -m venv .venv-design
.\.venv-design\Scripts\python -m pip install -r tools/design/requirements.txt
.\.venv-design\Scripts\python tools/design/build_specs.py
.\.venv-design\Scripts\python tools/design/build_specs.py --check
.\.venv-design\Scripts\python tools/design/validate_specs.py
.\.venv-design\Scripts\python tools/design/validate_specs.py --report
~~~

建议Python3.11/3.12独立环境。CI先安装依赖，再运行--check和validate；不要先自动生成再检查，否则掩盖未提交生成物漂移。虚拟环境、__pycache__和运行缓存不得提交。所有fixtures均模拟，无真实实验室图像或个人信息。

## 验证范围

验证失败exit非零；通过输出结构化计数。生成正例验证字段结构，负例验证required/未知字段及指定约束；不代表全部业务边界已覆盖。数据库检查为静态结构/FK类型/唯一目标，不等于MySQL实际执行。规则用例验证参考实现，不等于专家安全批准。完整应用用例见docs/07-quality-operations/01-testing-evaluation.md。

IRR快速检查可单独运行python -B tools/design/test_readiness_design.py；完整validate已自动包含。纯参考的object_allowed不是可部署IAM policy，public_endpoint不是SigV4验证器，quality_resized不执行Pillow resize；实际UP-03/SEC-03/AI-10仍须实施。
