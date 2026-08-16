# 从零开发一个 AICore 算子

## 1. 本讲目标

前两个单元里，我们已经「由内到外」读完了 add_example 的 host 三件套（u2-l2）和 AscendC 核函数（u2-l3）。本讲把视角反转：不再读现成算子，而是**亲手开发一个属于自己的 AICore 算子**。学完本讲，你应该能够：

1. 说出 AICore 算子从工程创建到验证的七步标准流程，以及每一步对应的交付件。
2. 用 `bash build.sh --genop=<分类>/<算子名>` 一键生成算子骨架，并理解脚手架背后 `opgen_standalone.py` 的「模板复制 + 改名 + 内容替换」机制。
3. 在生成的骨架上完成 def / infershape / tiling / kernel 四类文件的定制改造。
4. 完成编译出包、安装，并用 eager 示例验证算子正确性。
5. 了解如何把 Ascend/samples 仓的存量算子迁移到本项目的工程范式中。

## 2. 前置知识

本讲默认你已完成 u2-l2（op_host 三件套）和 u2-l3（op_kernel 核函数）的学习。开始前，请确认以下概念已经清晰：

- **五层算子范式**：一个算子目录由 op_host（host 侧信息库/推导/切分）、op_kernel（device 侧核函数）、examples（调用示例）、tests（UT）等子目录组成（见 u1-l2）。
- **def / infershape / tiling 的分工**：def 是静态户口（dtype/format 白名单、SoC 注册），infershape 在 aclnn 第一阶段推导输出 shape，tiling 产出 tiling data / tiling key / blockDim / workspace 报价（见 u2-l2）。
- **kernel 骨架**：薄入口（`__global__ __aicore__` + 五 GM 参数签名）+ 厚算子类（Init → Process → CopyIn/Compute/CopyOut），GM→UB→计算→UB→GM 的搬运循环（见 u2-l3）。
- **tiling key / tiling data**：tiling key 是运行期路由到编译期二进制变体的整数；tiling data 是 host 填、device 读的结构体「数据合同」。
- **编译入口**：build.sh 的 `--pkg --ops=<算子名>` 出 `.run` 包，`--run_example` 一键编译执行示例（见 u1-l4、u2-l4）。

另外两个本讲新引入的工具概念：

- **脚手架（scaffold）**：自动生成工程骨架的代码生成器。本项目用 `scripts/opgen/` 下的 Python 脚本实现，让你不必手抄目录结构和 CMakeLists。
- **算子工程迁移**：Ascend 官方 samples 仓里有大量按旧工程组织（单文件 op_host/{op_name}.cpp）写的算子，把它们搬进本项目需要按映射表拆分文件。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md) | AICore 算子开发官方指南：七步流程、交付件清单、UT 编写、samples 迁移映射表 |
| [scripts/opgen/opgen_standalone.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py) | 脚手架实现：从 `scripts/opgen/template/add_example` 模板复制生成新算子工程 |
| [scripts/opgen/template/add_example/](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/template/add_example/op_host/add_example_def.cpp) | 脚手架使用的「模板算子」，结构与 examples/add_example 一致但更精简 |
| [build.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh) | `--genop` 选项的解析（process_genop）与执行（gen_op） |
| [examples/add_example/op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp) | 完整版教学算子 def 文件（对照模板精读） |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp) | 完整版教学算子 tiling 实现 |
| [examples/add_example/op_kernel/add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp) | 完整版教学算子 kernel 入口 |
| [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp) | eager 调用示例（新算子验证时的仿写对象） |

## 4. 核心概念与源码讲解

### 4.1 算子开发全流程与最小交付件清单

#### 4.1.1 概念说明

官方开发指南把 AICore 算子开发总结为七步流水线：**工程创建 → 算子定义 → Tiling 实现 → Kernel 实现 → aclnn 适配 → 编译部署 → 算子验证**。这个流程的本质是「先搭骨架、再填语义、最后闭环验证」——目录和 CMake 由脚手架解决，算子功能语义由你填进 def/infershape/tiling/kernel 四类文件，aclnn 接口和二进制包则在编译时自动生成，不需要手写。

#### 4.1.2 核心流程

```text
① 工程创建     bash build.sh --genop=examples/my_sum
                    └─ 自动生成目录 + CMakeLists + 模板代码
② 算子定义     写 README.md + 改 ${op_name}_def.cpp（输入输出/dtype/format/SoC）
③ Tiling 实现  改 ${op_name}_tiling.cpp + tiling_key.h + tiling_data.h
④ Kernel 实现  改 ${op_name}.cpp（入口）+ ${op_name}.h（算子类）
⑤ aclnn 适配   无需手动操作——def 文件自动驱动二进制与 aclnn 接口生成
⑥ 编译部署     bash build.sh --pkg --soc=<soc> --ops=my_sum → 安装 .run 包
⑦ 算子验证     UT（无需 NPU）或 --run_example / aclnn 调用（需 NPU）
```

其中第⑤步值得特别注意：指南明确说「通过 ${op_name}_def.cpp 已自动生成算子二进制包，支持开发者直接使用」，也就是说 aclnn 接口是 def 注册的**自动收益**，而不是额外的交付件。

#### 4.1.3 源码精读

开发流程的权威定义在指南「开发流程」一节：

- [docs/zh/develop/aicore_develop_guide.md:22-34](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L22-L34)：以 `AddExample` 为例列出七步流程，并注明「如采用图模式调用算子，请参考图模式适配指南」——图模式是可选的第八步（u6-l2 展开）。

工程创建一节给出了 genop 命令和生成后的标准目录树：

- [docs/zh/develop/aicore_develop_guide.md:40-47](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L40-L47)：`bash build.sh --genop=${op_class}/${op_name}`，`${op_name}` 必须是小写下划线形式且不允许与已有算子重名。
- [docs/zh/develop/aicore_develop_guide.md:55-72](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L55-L72)：骨架目录树——examples（1 个 aclnn 示例）、op_host（def/infershape/tiling/CMakeLists）、op_kernel（tiling_key.h/tiling_data.h/入口 cpp/头文件 h）、算子根 CMakeLists。**这正是最小交付件清单**：9 个文件。
- [docs/zh/develop/aicore_develop_guide.md:74-95](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L74-L95)：一个容易踩的坑——若 `${op_class}` 是**全新分类**（不是 examples），需在 `cmake/custom_build.cmake` 中仿照 mc2 增加 `add_subdirectory`/目录探测语句，否则算子不会被编进构建树。放在 `examples/` 下则天然被识别。

各阶段交付件数量的官方口径（数一数就知道要写什么）：

| 阶段 | 交付件 | 位置 |
| --- | --- | --- |
| 算子定义 | README.md、`${op_name}_def.cpp` | 算子根 / op_host |
| Tiling | `${op_name}_tiling.cpp`、`${op_name}_tiling_key.h`、`${op_name}_tiling_data.h` | op_host / op_kernel |
| Kernel | `${op_name}.cpp`、`${op_name}.h` | op_kernel |
| 验证（可选） | infershape UT、tiling UT、kernel UT | tests/ut/（需手动创建，见 [docs/zh/develop/aicore_develop_guide.md:466-479](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L466-L479)） |

#### 4.1.4 代码实践

**实践：数交付件、验目录。**

1. 实践目标：把「最小交付件清单」从表格变成肌肉记忆。
2. 操作步骤：
   - 执行 `bash build.sh --genop=examples/my_sum`（详见 4.2.4 的完整实践，这里先跑这一步）；
   - 用 `find examples/my_sum -type f | sort` 列出生成的全部文件；
   - 对照上面的交付件表格逐个打勾。
3. 需要观察的现象：生成的文件数应是 9 个左右（含 CMakeLists），且**没有** infershape UT / tiling UT / tests 目录（tests 需手动创建，模板里只有一个 `.gitkeep` 占位，见 [scripts/opgen/template/add_example/tests/ut/.gitkeep](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/template/add_example/tests/ut/.gitkeep)）。
4. 预期结果：目录结构与指南 L55-L72 的树完全一致。
5. 本步骤产物将在 4.3、4.4 中继续使用，请勿删除。

#### 4.1.5 小练习与答案

**练习 1**：为什么 aclnn 适配不列为手动交付件？
**答案**：aclnn 接口和算子二进制包由 def 文件的注册信息在编译期自动生成（指南「aclnn适配」一节，[docs/zh/develop/aicore_develop_guide.md:395-399](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L395-L399)），开发者只需写好 def，无需额外配置。

**练习 2**：如果你把新算子放在全新分类 `myclass/` 下而不是 `examples/` 下，还需要做什么？
**答案**：需要修改 `cmake/custom_build.cmake`，仿照 mc2 的写法为 `myclass` 增加目录探测与 `add_subdirectory` 语句（指南 L74-95），否则编译系统找不到该算子。

### 4.2 opgen 脚手架：从模板到骨架的生成机制

#### 4.2.1 概念说明

`opgen` 是本项目的算子工程生成器。它的设计哲学非常朴素：**与其生成代码，不如复制模板**——仓库本来就维护着一个教学级模板算子 `add_example`（位于 `scripts/opgen/template/add_example`），新算子骨架 = 复制模板目录 + 把文件名和文件内容里的 `add_example` 字样替换成新算子名。这保证了「生成的骨架永远可编译」，也意味着**读懂 add_example 就等于会写所有新算子的起点**。

#### 4.2.2 核心流程

```text
build.sh --genop=examples/my_sum
  └─ process_genop: 拆 "examples/my_sum" → GENOP_TYPE=examples, GENOP_NAME=my_sum
  └─ gen_op: 调 python3 scripts/opgen/opgen_standalone.py -t examples -n my_sum -p <仓库根>
       └─ OpGenerator.run():
            1. _validate_inputs   校验名字合法（字母数字下划线）+ 目标目录不存在
            2. _copy_template     shutil.copytree(template/add_example → dest)
            3. _rename_files      文件/目录名中的 add_example → my_sum
            4. _replace_content   文件内容中的 add_example/AddExample/ADD_EXAMPLE → my_sum/MySum/MY_SUM
```

三种名字形态的替换规则（还原为命名约定）：

| 模板中的形态 | 替换为 | 用于 |
| --- | --- | --- |
| `add_example` | `my_sum` | 文件名、kernel 入口函数名 |
| `AddExample` | `MySum` | 算子类名、`OP_ADD`/`IMPL_OP_*` 注册名 |
| `ADD_EXAMPLE` | `MY_SUM` | 大写宏（如头文件保护宏） |

#### 4.2.3 源码精读

- [build.sh:1025-1056](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1025-L1056)：`process_genop` 负责解析 `--genop=` 的值——必须恰好含一个 `/`（`op_class/op_name`），多了少了都打回 usage；随后拆出 `GENOP_NAME`（`##*/`）和 `GENOP_TYPE`。
- [build.sh:1058-1079](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1058-L1079)：`gen_op` 探测 python3/python 后执行 `opgen_standalone.py -t ${GENOP_TYPE} -n ${GENOP_NAME} -p ${GENOP_BASE}`——脚手架完全由这个独立 Python 脚本承担，build.sh 只是转发参数。
- [scripts/opgen/opgen_standalone.py:22-40](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L22-L40)：`OpGenerator.__init__` 锁定模板源——`template_variant == "aicpu"` 时用 `template/add_example_aicpu`（u2-l5 的 AICPU 骨架），否则用 `template/add_example`；目标目录为 `<output_path>/<op_type>/<op_name>`。
- [scripts/opgen/opgen_standalone.py:51-67](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L51-L67)：输入校验——分类名和算子名都只允许 `^[a-zA-Z0-9_]+$`（防止路径穿越），且目标目录已存在即抛 `FileExistsError`，保证幂等安全。
- [scripts/opgen/opgen_standalone.py:82-97](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L82-L97)：`_rename_files` 自底向上（`topdown=False`）遍历，把名字里含 `add_example` 的文件和目录改名——先改子目录里的文件、再改目录，避免父目录改名后路径失效。
- [scripts/opgen/opgen_standalone.py:121-142](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L121-L142)：`_replace_content` 构造四种替换对（含按 `_` 分词后 `capitalize` 拼接的驼峰形态），对所有文本文件做字符串替换；`.pyc/.pyo` 跳过。**注意替换是全局字符串替换**，所以模板中任何出现 `add_example` 的注释、字符串也会被换名——这也是模板必须保持「名字即占位符」纪律的原因。
- [scripts/opgen/opgen_standalone.py:156-175](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L156-L175)：`register_parser` 暴露的独立 CLI 参数：`-t/--op_type`、`-n/--op_name`、`-p/--output_path`、`-v/--template_variant`（default/aicpu）——也就是说你可以绕过 build.sh 直接调脚本，效果等同。

#### 4.2.4 代码实践

**实践：生成 my_sum 骨架并观察替换效果。**

1. 实践目标：直观看到「模板 → 骨架」的改名与替换。
2. 操作步骤：
   ```bash
   # 在仓库根目录执行
   bash build.sh --genop=examples/my_sum
   # 观察 build.sh 转发给脚本的参数与成功提示
   ls examples/my_sum examples/my_sum/op_host examples/my_sum/op_kernel
   grep -rn "AddExample\|add_example" examples/my_sum || echo "残留检查通过：无 add_example 字样"
   grep -n "MySum" examples/my_sum/op_host/my_sum_def.cpp | head -5
   ```
3. 需要观察的现象：成功提示 `Create the initial directory for my_sum under examples success`（由 [build.sh:1064-1065](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1064-L1065) 的 echo 与脚本内 logging 共同输出）；`grep` 残留检查应输出「残留检查通过」。
4. 预期结果：`my_sum_def.cpp` 中类名变为 `MySum`，注册宏变为 `OP_ADD(MySum)`；再次执行同一命令会因目录已存在而报错退出（幂等保护）。
5. 以上行为基于源码逻辑推断，输出细节待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果我想生成 AICPU 版骨架，应该怎么调用？脚本内部走哪个分支？
**答案**：`bash build.sh --genop_aicpu=examples/my_aicpu_op`；脚本内 `template_variant == "aicpu"` 时模板目录切到 `template/add_example_aicpu`（[scripts/opgen/opgen_standalone.py:29-36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L29-L36)）。

**练习 2**：为什么 `_rename_files` 要用 `topdown=False` 自底向上遍历？
**答案**：目录名本身也可能含 `add_example` 需要改名；若先改父目录名，先前记录的子路径就失效了。自底向上保证改子目录内的文件时路径仍然有效（[scripts/opgen/opgen_standalone.py:84-97](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L84-L97)）。

### 4.3 交付件逐个落实：把骨架改造成「沿最后一维求和」的 my_sum

#### 4.3.1 概念说明

骨架生成后，所有文件语义上仍是「逐元素相加」。本节以目标算子 **my_sum（输入 x，沿最后一维求和，输出 y，输出 shape 为输入去掉最后一维）** 为例，讲清每个交付件需要动哪些地方。选这个算子是有意为之：它是**归约（reduction）算子**，与 add_example 的逐元素（elementwise）形态在 tiling 与 kernel 两层都不同，能逼你真正理解而不是改名了事。

先明确数学语义。设输入 \( x \in \mathbb{R}^{B \times D} \)（以 2 维为例，更高维同理按行处理），则：

\[ y_i = \sum_{j=0}^{D-1} x_{i,j}, \quad i = 0, 1, \dots, B-1 \]

输出 shape 为 \( (B,) \)。与逐元素算子的关键差异：**输出数据量比输入小一个维度**，infershape 不能再拷贝输入 shape，kernel 里每处理一行只在 GM 写回一个数。

#### 4.3.2 核心流程

改造清单（★ = 必改，☆ = 通常可沿用模板）：

```text
op_host/my_sum_def.cpp          ★ 输入从 x1/x2 改为单个 x；输出 dtype 与输入一致；
                                  ExtendCfgInfo("opFile.value","my_sum") 已由脚手架替换好
op_host/my_sum_infershape.cpp   ★ 输出 shape = 输入 shape 去掉最后一维
op_host/my_sum_tiling.cpp       ★ 重新定义切分单位：按「行」分给各核；
                                  tiling data 增加 rowLength/rowNum 等字段
op_kernel/my_sum_tiling_data.h  ★ 结构体字段随 tiling 策略调整
op_kernel/my_sum_tiling_key.h   ☆ 单 dtype（如仅 fp32）时保留一个 schMode 即可
op_kernel/my_sum.cpp            ★ 入口签名改为 (x, y, workspace, tiling)
op_kernel/my_sum.h              ★ 算子类重写：CopyIn 整行搬入，Compute 归约求和，CopyOut 写单个元素
examples/test_aclnn_my_sum.cpp  ★ 单输入构造 + 期望值 = 行和
```

#### 4.3.3 源码精读

**（1）def 文件：改输入输出原型。** 模板三件里的 def 是这样描述双输入逐元素算子的：

- [examples/add_example/op_host/add_example_def.cpp:22-39](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L22-L39)：两个 REQUIRED 输入 `x1`/`x2` 与一个输出 `y`，dtype 白名单 `{DT_FLOAT, DT_INT32}` 按下标配对，`AutoContiguous()` 保证非连续输入自动连续化。my_sum 需删掉 `x2`，白名单可以先只留 `ge::DT_FLOAT`。
- [examples/add_example/op_host/add_example_def.cpp:41-51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L41-L51)：`OpAICoreConfig` 打开动态 shape/动态 rank 支持；`ExtendCfgInfo("opFile.value", "add_example")` 把 host 定义与 kernel 入口文件名挂钩（u2-l1 讲过的 host↔kernel 连接点，脚手架已把它替换成 `my_sum`）；三个 `AddConfig` 注册 ascend910b / ascend910_93 / ascend950 三代 SoC——初次开发可以先只保留你手头硬件对应的一项。
- [examples/add_example/op_host/add_example_def.cpp:54](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L54)：`OP_ADD(AddExample)` 把整个原型注入算子信息库——生成后这里是 `OP_ADD(MySum)`，名字错了编译期就会失败，因此是最快的自检点。

**（2）infershape：从「拷贝」改为「裁剪」。**

- [examples/add_example/op_host/add_example_infershape.cpp:23-45](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_infershape.cpp#L23-L45)：模板逻辑是逐维拷贝输入 shape 到输出。my_sum 的改造（示例代码）：

```cpp
// 示例代码：my_sum 的 infershape 核心逻辑
auto xShapeSize = xShape->GetDimNum();
yShape->SetDimNum(xShapeSize - 1);              // 输出比输入少最后一维
for (size_t i = 0; i < xShapeSize - 1; i++) {
    yShape->SetDim(i, xShape->GetDim(i));
}
```

  注册入口 [examples/add_example/op_host/add_example_infershape.cpp:47](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_infershape.cpp#L47) 的 `IMPL_OP_INFERSHAPE(AddExample).InferShape(...)` 同理由脚手架替换成了 `MySum`。

**（3）tiling：切分单位从「元素」改为「行」。** 先看模板的三段式结构，再谈改法：

- [examples/add_example/op_host/add_example_tiling.cpp:40-51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L40-L51)：`GetPlatformInfo` 通过 `PlatformAscendC` 拿 AIV 核数与 UB 大小——my_sum 沿用不变。
- [examples/add_example/op_host/add_example_tiling.cpp:54-91](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L54-L91)：`GetShapeAttrsInfo` 取 shape/dtype 并校验 4 维限制。my_sum 这里改为取「行数 = 前面各维乘积，行长 = 最后一维」，并可放松 DIMS_LIMIT。
- [examples/add_example/op_host/add_example_tiling.cpp:102-141](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L102-L141)：`AddExampleTilingFunc` 主入口——填 tiling data（`totalLength`/`tileNum`）、`SetBlockDim(8)`、按 dtype 用 `GET_TPL_TILING_KEY` 设置 tiling key。my_sum 的关键改造点（示例代码）：

```cpp
// 示例代码：my_sum 的 tiling data 填充思路
tiling->rowNum = rowNum;          // 总行数（= 输出元素个数）
tiling->rowLength = rowLength;    // 每行元素个数
tiling->rowsPerBlock = (rowNum + blockDim - 1) / blockDim;  // 每核负责的行数（向上取整）
```

  注意与 add_example 的本质区别：逐元素算子把**元素**均分到核；行归约算子把**行**均分到核，核内对每行做长度为 rowLength 的累加。行长超过单块 UB 容量时还需在核内分行分段搬入（切 D 搬运），第一版可先限定 `rowLength * sizeof(T) <=` 单块 UB 上限来简化。
- [examples/add_example/op_host/add_example_tiling.cpp:143-149](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L143-L149)：`TilingParseForAddExample` 是图模式标准交付件，手写 aclnn 可置空直接返回 `GRAPH_SUCCESS`；最后的 `IMPL_OP_OPTILING(...).Tiling(...).TilingParse<...>(...)` 完成 tiling 注册。

**（4）tiling data / tiling key：数据合同与路由表。**

- [examples/add_example/op_kernel/add_example_tiling_data.h:19-22](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L22)：普通 C++ 结构体，两个字段。my_sum 按上面示例改为 `rowNum/rowLength/rowsPerBlock` 等字段。这个文件同时被 host（填充方）和 device（消费方）include，是「一份结构、两端使用」的合同（u2-l3）。
- [examples/add_example/op_kernel/add_example_tiling_key.h:21-28](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example_tiling_key.h#L21-L28)：`ASCENDC_TPL_ARGS_DECL/ASCENDC_TPL_SEL` 声明名为 `schMode` 的模板参数，取值 0/1 对应 fp32/int32 两条 kernel 实例路径。my_sum 若第一版只支持 fp32，可把候选值裁到只剩 `ELEMENTWISE_TPL_SCH_MODE_0`。

**（5）kernel：入口变薄、算子类重写。**

- [examples/add_example/op_kernel/add_example.cpp:18-38](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel/add_example.cpp#L18-L38)：入口三步走——`REGISTER_TILING_DEFAULT` 注册结构体、`GET_TILING_DATA_WITH_STRUCT` 解包 tiling、`if constexpr (schMode == ...)` 按 tiling key 实例化 `NsAddExample::AddExample<float>` 并执行 `Init/Process`。my_sum 的入口只需把双输入签名 `(x, y, z, workspace, tiling)` 改成单输入 `(x, y, workspace, tiling)`，并删掉 int32 分支。
- 算子类（在 `my_sum.h`）中的 Process 循环从「CopyIn(i) → Compute(i) → CopyOut(i)」改造为（示例代码，伪代码）：

```cpp
// 示例代码：my_sum 算子类 Process 伪代码
for (int32_t r = 0; r < rowsPerBlockThisCore; r++) {   // 本核负责的每一行
    CopyIn(r);          // 整行 x[row, :] 从 GM 搬到 UB
    sum = ReduceRow();  // 在 UB 内沿行长累加（可用 Ascend C 归约指令或标量累加，
                        // 具体指令选型请查《Ascend C算子开发接口》文档，待确认）
    CopyOut(r);         // 单个累加结果写回 GM 的 y[row]
}
```

  指令选型说明：Ascend C 提供向量归约类接口（如 `WholeReduceSum` 系列），不同 CANN 版本接口名与可用性有差异，落地前请以你配套版本的《Ascend C算子开发接口》为准（指南 L13-14 给出的官方文档入口）；最保守的实现是用 `ReduceMax/ReduceSum` 风格接口退化为标量循环累加，正确性优先、性能其次。

**（6）eager 示例：单输入 + 行和期望值。**

- [examples/add_example/examples/test_aclnn_add_example.cpp:87-115](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L87-L115)：示例 main 的构造段——device/stream 初始化后，按 `{32,4,4,4}` 构造两个输入和一个输出。my_sum 改为构造单个输入（如 shape `{8, 16}` 的 fp32）与 shape `{8}` 的输出，头文件换成 `#include "aclnnop/aclnn_my_sum.h"`（由安装后的算子包生成）。
- [examples/add_example/examples/test_aclnn_add_example.cpp:118-120](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/examples/test_aclnn_add_example.cpp#L118-L120)：两段式调用的第一段——`workspaceSize` 与 `executor` 就绪后进入 `aclnnAddExample` 下发。my_sum 对应调用 `aclnnMySumGetWorkspaceSize` / `aclnnMySum`，骨架完全一致（u2-l4）。

#### 4.3.4 代码实践

**实践：完成 my_sum 的 def/infershape/tiling/kernel 四件改造（本讲主实践，与 4.2.4 衔接）。**

1. 实践目标：在生成骨架上把逐元素加法改造为「沿最后一维求和」。
2. 操作步骤：
   1. 改 `my_sum_def.cpp`：删除 `x2` 输入；dtype 白名单仅留 `ge::DT_FLOAT`；确认 `ExtendCfgInfo("opFile.value", "my_sum")` 与 `OP_ADD(MySum)` 正确。
   2. 改 `my_sum_infershape.cpp`：输出 shape 为输入去掉最后一维（见上文示例代码）。
   3. 改 `my_sum_tiling_data.h`：字段改为 `rowNum/rowLength/rowsPerBlock`。
   4. 改 `my_sum_tiling.cpp`：`GetShapeAttrsInfo` 解析出行数/行长；`TilingFunc` 填充新字段并 `SetBlockDim`（先用模板的 8）。
   5. 改 `my_sum.h`：算子类按「行」循环，Compute 内累加整行，CopyOut 只写一个元素。
   6. 改 `my_sum.cpp`：入口签名删掉一个 GM 参数，仅保留 fp32 分支。
   7. 改 `examples/test_aclnn_my_sum.cpp`：单输入 `{8,16}`（每行填 1.0，则期望输出每元素 16.0）。
3. 需要观察的现象：编译阶段若 def 类名/注册名/`opFile.value` 任一不匹配会在链接或打包时报错；运行阶段输出 8 个 `16.0` 即正确。
4. 预期结果：`y = [16, 16, ..., 16]`（8 个）。编译与运行命令见 4.4.4。
5. 本实践含真实运行环节，输出结果待本地验证（无 NPU 时可用 4.4 介绍的 simulator 或 UT 路线）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 my_sum 的 tiling 不能直接沿用 `totalLength = 元素总数` 均分到核？
**答案**：归约算子的最小独立单元是「行」而不是元素。若按元素均分，一行可能被切给多个核，各核只拿到部分和，还需要二次归并跨核通信；按行分配让每行完整落在单核内，核内累加即可写出最终结果。

**练习 2**：如果行长 rowLength 很大（如 65536 个 fp32，约 256KB）超过单块 UB 预算，tiling 应该怎么调整？
**答案**：在核内对行再做分段（切 D 搬运）：每段搬入 UB 累加到部分和寄存器/局部变量，全部段完成后一次写回。tiling data 需增加 `segLength`（每段元素数）之类的字段，host 侧按 UB 大小算出安全的段长（参考 `GetPlatformInfo` 拿到的 `ubSize`，[examples/add_example/op_host/add_example_tiling.cpp:40-51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L40-L51)）。这一模式与 u5-l1 讲过的 MoE 重排算子「整行/切 D 两种搬运模式」同源。

**练习 3**：`TilingParseForAddExample` 是空实现，为什么还必须保留？
**答案**：它是图模式标准交付件，框架按注册名约定回调该函数；自动生成 aclnn 不调用它，但删掉会导致注册不完整。指南对此有明确说明（[docs/zh/develop/aicore_develop_guide.md:150-153](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L150-L153)）。

### 4.4 编译部署、验证与算子工程迁移

#### 4.4.1 概念说明

写完代码只是上半场。算子要以 `.run` 自解压安装包的形式部署到 `${ASCEND_HOME_PATH}/opp/vendors/<vendor_name>_transformer` 下，aclnn 接口才会生效。验证有三条路径，按「是否需要 NPU」分层：**UT 验证**（CPU 仿真，无需 NPU）、**aclnn 调用验证**（需 NPU，或用 simulator 仿真）、以及贯穿两者的 `--run_example` 一键入口（u2-l4）。最后，「从 Ascend/samples 仓迁移存量算子」是另一条获得算子代码的路径，官方给出了逐文件映射表。

#### 4.4.2 核心流程

```text
编译部署：
  source /usr/local/Ascend/cann/set_env.sh          # 环境变量（每次新会话）
  bash build.sh --pkg --soc=ascend910b --ops=my_sum # 出 .run 包到 build_out/
  ./build_out/cann-ops-transformer-custom_linux-<arch>.run   # 安装到 vendors/

验证：
  路线A（无 NPU）: 补 tests/ut/ 三件 UT → bash build.sh --ophost_test/--opkernel_test --ops=my_sum
  路线B（NPU/simulator）: bash build.sh --run_example my_sum eager [--simulator=camodel --soc=ascend950]

samples 迁移：
  samples 的单文件 op_host/{op}.cpp ──拆分──> def + infershape + tiling (+ graph_infer)
  samples 的宏定义 TilingData          ──改写──> 标准 C++ 结构体 tiling_data.h
  samples 的核函数                     ──增强──> REGISTER_TILING_DEFAULT + 模板参数 + tiling_key.h
```

#### 4.4.3 源码精读

- [docs/zh/develop/aicore_develop_guide.md:413-429](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L413-L429)：编译命令完整形态 `bash build.sh --pkg --soc=${soc_version} --vendor_name=${vendor_name} --ops=${op_list}`，并列出 soc 参数与三代硬件的对应（ascend910b / ascend910_93 / ascend950）。
- [docs/zh/develop/aicore_develop_guide.md:436-453](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L436-L453)：成功标志 `Self-extractable archive "cann-ops-transformer-...run" successfully created`；安装后位于 `opp/vendors`；**注意 run 包不支持卸载**，要删只能删 vendors 目录并清理 config.ini 的 load_priority 项。
- [docs/zh/develop/aicore_develop_guide.md:457-461](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L457-L461)：两条验证路线的官方定义——UT 无需 NPU、aclnn 需 NPU。UT 三件（infershape/tiling/kernel）的目录需手动创建（[docs/zh/develop/aicore_develop_guide.md:466-479](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L466-L479)），写法在 u7-l1 展开。
- [docs/zh/develop/aicore_develop_guide.md:735-740](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L735-L740)：aclnn 验证前需把 vendors 下的 op_api/lib 加入 `LD_LIBRARY_PATH`，否则运行时找不到算子动态库。
- [docs/zh/develop/aicore_develop_guide.md:746-804](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L746-L804)：**samples 迁移映射表**，核心四条：
  1. samples 的 `op_host/{op}.cpp` 是「一锅炖」文件，要拆成 def（去掉 `SetInferShape`/`SetTiling`）、infershape（可选，配 `IMPL_OP_INFERSHAPE`）、tiling（只留 TilingFunc，配 `IMPL_OP_OPTILING`）、graph_infer（可选，配 `IMPL_OP`）。
  2. 宏定义的 TilingData（`BEGIN_TILING_DATA_DEF` 那套）要改写成 `op_kernel/{op}_tiling_data.h` 里的标准 C++ 结构体，赋值方式从 `tiling.set_xxx` 变为直接成员赋值（对照 [docs/zh/develop/aicore_develop_guide.md:927-952](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L927-L952) 的前后对比）。
  3. 核函数要**新增** `REGISTER_TILING_DEFAULT` + `GET_TILING_DATA_WITH_STRUCT` 两条宏，并把硬编码分支升级为模板参数分支（[docs/zh/develop/aicore_develop_guide.md:1035-1056](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L1035-L1056)）。
  4. tiling key 文件从 samples 的 `tiling_key_{op}.h` 迁到 `{op}_tiling_key.h`，不存在则参照 add_example 新增。
- [docs/zh/develop/aicore_develop_guide.md:1065-1069](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L1065-L1069)：跨平台迁移（如 A2 → 950）另有专门文档 `cross_platform_migration_guide.md`，对应 u4-l3 讲过的 arch22/arch35 目录隔离机制。

#### 4.4.4 代码实践

**实践：把 my_sum 编译、部署并验证（4.3.4 的收尾）。**

1. 实践目标：走完「代码 → .run 包 → 安装 → eager 验证」最后一公里。
2. 操作步骤：
   ```bash
   # ① 环境变量（按实际安装路径）
   source /usr/local/Ascend/cann/set_env.sh
   # ② 编译出包（按手头硬件选 soc；无 NPU 也可编译）
   bash build.sh --pkg --soc=ascend910b --ops=my_sum
   # ③ 安装
   ./build_out/cann-ops-transformer-custom_linux-$(uname -m).run
   # ④ 一键编译并运行 eager 示例（需 NPU；或加 --simulator=camodel --soc=ascend950 走仿真）
   bash build.sh --run_example my_sum eager
   ```
3. 需要观察的现象：②输出 `Self-extractable archive ... successfully created`；④示例打印 8 个 `16.0`。
4. 预期结果：输出与 4.3.4 设定的期望一致；若第一段 `GetWorkspaceSize` 返回非 0，按 u3-l1 的返回号段分流法排查（161xxx 查传参）。
5. 无 NPU 环境时：编译出包可以完成（编译态不依赖硬件，见 u1-l3），运行环节待本地验证；替代路线是按指南补 kernel UT 后用 `bash build.sh --opkernel_test --ops=my_sum` 在 CPU 仿真验证（ICPU_RUN_KF 机制，见 u2-l3/u3-l4）。

#### 4.4.5 小练习与答案

**练习 1**：安装后的 run 包想撤销重装，该怎么做？
**答案**：run 包没有卸载器，需手动删除 `vendors/<vendor_name>` 目录，并删除 `vendors/config.ini` 中 load_priority 对应配置项（[docs/zh/develop/aicore_develop_guide.md:451-453](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L451-L453)）。

**练习 2**：samples 仓一个算子的 TilingData 用 `BEGIN_TILING_DATA_DEF` 宏定义、用 `tiling.set_totalLength(...)` 赋值，迁到本项目后要改成什么样？
**答案**：结构体改为 `op_kernel/{op}_tiling_data.h` 里的标准 C++ struct；TilingFunc 中改为 `context->GetTilingData<T>()` 拿指针后直接成员赋值（`tiling->totalLength = ...`），并删除 `SaveToBuffer/SetDataSize` 那套手动序列化（[docs/zh/develop/aicore_develop_guide.md:987-1001](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L987-L1001)、[docs/zh/develop/aicore_develop_guide.md:927-952](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L927-L952)）。

## 5. 综合实践

**任务：交付一个可验证的自定义归约算子 my_sum，并写一份「开发日志」。**

把 4.2.4 → 4.3.4 → 4.4.4 三个实践串成一个完整任务：

1. 用 `--genop` 生成骨架，`git status` 查看新增文件数并与最小交付件清单核对。
2. 完成 def/infershape/tiling_data/tiling/kernel/示例六处改造，每改一处提交一次 git commit，提交信息写明「改了哪个交付件、为什么」。
3. 编译出包并安装；有 NPU 则 `--run_example` 验证 `{8,16}` 全 1 输入输出全 16；无 NPU 则改造成 UT 路线（`tests/ut/op_kernel/test_my_sum.cpp`，用 `ICPU_RUN_KF` + 行和期望值比对）。
4. 挑战加分项：给 my_sum 增加第二个 dtype（fp16），体会「tiling key 加一档 + kernel 加一个 `if constexpr` 分支 + def 白名单加一项」的**三点联动**——这正是 u4-l3 工业级算子多 dtype 变体的最小雏形。
5. 最后写一份开发日志：记录每个交付件的改动行数、踩到的编译/运行报错及解法。这份日志就是你未来贡献真实算子（u7-l3）的草稿。

## 6. 本讲小结

- AICore 算子开发是七步流水线：工程创建 → 算子定义 → Tiling → Kernel → aclnn 适配（自动） → 编译部署 → 验证；最小交付件共 9 个文件，外加可选的 UT 三件。
- `--genop` 的本质是「复制 `scripts/opgen/template/add_example` 模板 + 三种命名形态的全局替换」，由 `opgen_standalone.py` 独立完成，build.sh 只做参数拆解与转发。
- 新算子放在 `examples/` 分类下零 CMake 改动；放全新分类必须改 `cmake/custom_build.cmake`，否则进不了构建树。
- 归约算子与逐元素算子的核心差异在两处：infershape 从「拷贝 shape」变为「裁剪维度」，tiling 的切分单位从「元素」变为「行」（行长超 UB 还需核内分段）。
- aclnn 接口与二进制包是 def 注册的自动收益；验证分 UT（无 NPU）与 aclnn/run_example（需 NPU 或 simulator）两条路线。
- Ascend/samples 迁移 = 单文件拆四件 + 宏 TilingData 改标准结构体 + 核函数补两条 tiling 宏与模板参数分支。

## 7. 下一步学习建议

- **u6-l2（op_graph 与图融合入门）**：本讲的 my_sum 只支持 eager 调用；若要让算子进 GE 图，需补 `op_graph/{op}_proto.h` 与 `graph_infer.cpp`，指南中置空的 `TilingParse` 也随之有了真实职责。
- **u6-l4（调试与调优）**：my_sum 跑通后，用 profiler 看它在大 shape 下的耗时瓶颈，尝试调整 blockDim/tileNum 并对比数据；再用 `--dump_cce` 观察中间代码。
- **u7-l1（单元测试体系）**：为 my_sum 补齐 infershape/tiling/kernel 三件 UT，把本讲的「可选验证」变成 CI 门禁。
- **源码延伸阅读**：把 `examples/add_example` 与 `scripts/opgen/template/add_example` 做 `diff`，观察模板与完整教学算子的差异——你会看到 tests、op_graph、README 等「正式交付件」与「骨架最小集」的分界线。
