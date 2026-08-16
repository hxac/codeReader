# 目录结构与模块地图

## 1. 本讲目标

上一讲我们认识了 ops-transformer 的定位（CANN 面向 transformer 大模型的进阶算子库）。本讲解决一个更具体的问题：**面对一个包含数百个算子、顶层目录几十个的仓库，如何快速建立「地图感」**。学完本讲，你应该能够：

1. 画出仓库顶层目录的分类地图，说出每类目录的职责。
2. 解释单个算子目录的标准结构（op_host / op_api / op_kernel / op_graph / tests / examples 五层范式），以及每一层各自被谁编译、被谁调用。
3. 判断一个算子目录「缺了某一层」意味着什么（不支持 aclnn？不支持图模式？复用了别的算子实现？）。

## 2. 前置知识

本讲不需要写代码，但需要几个上一讲引入的术语，这里简要回顾：

- **Host 侧 / Device 侧**：算子代码分两半。Host 侧（CPU 上运行）负责算子注册、shape 推导、切分策略计算；Device 侧（NPU 上运行）就是真正的核函数（kernel），做实际的数据计算。
- **aclnn 接口**：CANN 提供的两段式 C 接口（`aclnnXxxGetWorkspaceSize` + `aclnnXxx`），用户可以在不构图的情况下直接调用算子，这种方式叫 **Eager（急切）模式**。
- **图模式（Graph / GE）**：把算子加进计算图，由图引擎统一调度执行，`op_graph` 目录服务于这条路。
- **SoC / soc_version**：NPU 芯片型号，如 ascend910b（A2）、ascend910_93（A3）、ascend950（A5）。不同代际芯片的 kernel 实现可能不同，所以目录里会出现按架构划分的子目录。

如果这些概念还模糊，不影响本讲阅读——本讲只讲「目录为什么这么组织」，细节会在后续单元展开。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| `docs/zh/install/dir_structure.md` | 官方目录结构说明文档，是本讲的「教材」，详细解释了每一层目录和每种文件的含义 |
| `README.md` | 项目总介绍，其中「更多资料」一节链接了目录结构文档；Latest News 展示了 framework 插件目录的用法 |
| `attention/flash_attention_score/` | 样本算子 1：层级最全的工业级算子目录 |
| `moe/moe_token_permute/` | 样本算子 2：缺少 op_api 与 op_graph 的算子，用来理解「缺层」的含义 |
| `mc2/matmul_all_reduce/` | 样本算子 3：多版本接口共存的通信融合算子 |

## 4. 核心概念与源码讲解

### 4.1 仓库目录结构

#### 4.1.1 概念说明

ops-transformer 的顶层目录可以分成四类：

1. **算子域目录**（业务分类）：`attention`、`moe`、`mc2`、`ffn`、`gmm`、`mhc`、`mamba`、`posembedding`。每个目录下是同领域的一组算子工程，例如 `attention` 下有 70+ 个 attention 类算子（flash_attention_score、fused_infer_attention_score、lightning_indexer 等）。
2. **公共能力目录**：`common`（公共头文件与公共实现）、`3rdparty`（第三方依赖）。
3. **工程设施目录**：`scripts`（构建/脚手架/CI 脚本）、`cmake`（CMake 模块）、`tests`（项目级测试）、`docs`（项目文档）、`torch_extension`（PyTorch 扩展包）。
4. **用户扩展目录**：`examples`（端到端示例，含教学算子 add_example 与 fast_kernel_launch 示例）、`experimental`（用户自定义算子与工程模板的实验区）。

再加根目录下的 `build.sh`（编译入口）、`CMakeLists.txt`（工程 CMake 入口）、`classify_rule.yaml`（组件划分）、`version.info` / `version.cmake`（版本信息）等工程文件。

#### 4.1.2 核心流程

拿到任何一个 CANN 算子仓库，都可以按下面的顺序建立地图：

```text
1. 看 README.md        → 项目定位、支持矩阵、文档入口
2. 看 docs/            → 安装、调用、开发、调试四大类文档
3. 看算子域目录        → 按业务找到目标算子工程
4. 进入单个算子目录    → 按「五层范式」逐层阅读
5. 看 build.sh         → 了解这些目录如何被编译串联
```

#### 4.1.3 源码精读

官方文档对顶层目录的划分有明确说明，见目录树中的注释：[docs/zh/install/dir_structure.md:L13-L24](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L13-L24)——这里定义了 `cmake`（编译模板）、`common`（公共代码，分 `inc` 头文件与 `src` 实现）、`experimental`（用户自定义算子存放目录）三类公共目录。

项目 README 的「更多资料」一节直接链接了该目录结构文档：[README.md:L58](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L58)，说明它是官方推荐的阅读起点。

目录树后半部分列出了其余顶层目录的职责：[docs/zh/install/dir_structure.md:L70-L104](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L70-L104)——`docs` 是文档目录、`examples` 是端到端算子开发和调用示例、`scripts` 是构建与脚手架脚本、`tests` 是项目级测试、`torch_extension` 是开放 PyTorch 扩展 API 的包，其中 `torch_extension/cann_ops_transformer` 下又分为 `common`、`op_builder`（管理 JIT 编译与 schema/meta 注册）、`ops`（每个算子一个 Python 前端文件）等子目录。

值得一提的是 `examples` 目录不止有教学算子，README 的 Latest News 还提到了 fast_kernel_launch（简易算子）调用示例：[README.md:L11](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L11)，对应仓库中实际存在的 `examples/fast_kernel_launch_example` 目录；而 `framework` 子目录（如 `attention/flash_attention_score/framework` 下的 ONNX 插件）则见 [README.md:L15](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L15)——这是五层范式之外的可选第六层。

#### 4.1.4 代码实践

**实践目标**：不借助 IDE，用命令行在 3 分钟内说出仓库的顶层目录分类。

**操作步骤**：

1. 在仓库根目录执行 `ls`，对照上文四类分类，把每个目录归入「算子域 / 公共 / 工程设施 / 用户扩展」。
2. 进入一个算子域目录（如 `ls attention`），数一数有多少个算子工程，挑两个看名字猜测功能，再打开它们的 README 验证猜测。
3. 执行 `ls common/inc common/src scripts`，确认公共库与脚本目录的实际内容与文档描述一致。

**需要观察的现象**：算子域目录下全部是「算子工程目录」（目录名即算子名，小写下划线形式），没有散乱的源文件；`scripts` 下有 `opgen`（脚手架）、`ci`、`kernel`、`package` 等子目录。

**预期结果**：能口述一张四类地图，并说出 `attention` 域下任意两个算子的名字与用途。

#### 4.1.5 小练习与答案

**练习 1**：`common` 和 `attention/common`（如果存在）有什么区别？

**答案**：`common` 是全仓库共享的公共库，由 `common/CMakeLists.txt` 统一编译，供所有算子域引用；`attention/common` 这类域内 common 只在 attention 域内的算子间复用，作用域更小。两者是「全局复用」与「域内复用」的分层。

**练习 2**：我想找「通信-计算融合」类算子，应该去哪个顶层目录？为什么 `torch_extension` 不算算子域目录？

**答案**：去 `mc2`。`torch_extension` 是把已有 aclnn 算子包装成 PyTorch API 的扩展包（Python 前端 + C++ 绑定），它自己不定义新的算子工程（没有 op_host/op_kernel），所以属于工程设施而非算子域。

### 4.2 算子目录范式（五层结构）

#### 4.2.1 概念说明

每个算子工程目录遵循统一的「五层范式」：

| 子目录 | 侧 | 职责 | 缺失含义 |
|---|---|---|---|
| `op_host` | Host | 算子信息库（def：名称、输入输出、数据类型）、InferShape、Tiling 切分策略 | 复用了其他算子的 op_host 实现，或暂无 Ascend C 实现 |
| `op_api` | Host | aclnn 接口实现（参数校验、两段式调用分发） | 暂不支持 aclnn 直接调用 |
| `op_kernel` | Device | Ascend C 核函数（真正的计算逻辑） | 复用了其他算子的 kernel，或暂无实现 |
| `op_graph` | Host | 图模式的算子原型声明（proto）、数据类型推导（graph_infer）、融合规则（fusion_pass） | 暂不支持图模式调用 |
| `tests` | — | UT 用例与精度测试数据 | — |
| `examples` | — | aclnn / geir 调用示例程序 | — |

「缺失」一列不是我们猜的，官方文档开头的注意事项明确列出了这四条规则：[docs/zh/install/dir_structure.md:L5-L11](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L5-L11)。注意其中微妙的差别：**缺 op_host 或 op_kernel 的常见原因是「调用别的算子的实现」**（看该算子 op_api/op_graph 源码即可找到调用逻辑），而**缺 op_api/op_graph 就是明确不支持对应调用方式**。

#### 4.2.2 核心流程

一个算子从源码到运行的分工：

```text
用户程序
  ├── Eager 路径：调用 examples/test_aclnn_${op}.cpp 风格代码
  │       └── op_api/aclnn_${op}.cpp   （校验 + 分发）
  │               └── op_host/*_tiling.cpp  （算切分策略）
  │                       └── op_kernel/${op}.cpp  （NPU 上执行计算）
  └── Graph 路径：算子进入 GE 计算图
          └── op_graph/${op}_proto.h  （图里识别算子的原型）
                  └── 同样落到 op_host → op_kernel

tests/  负责验证以上两条路的正确性
```

#### 4.2.3 源码精读

官方文档给出了单个算子目录的完整模板：[docs/zh/install/dir_structure.md:L25-L38](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L25-L38)。注意目录命名约定：算子目录和文件用小写下划线（`${op_name}`），而接口文档用大驼峰（`docs/aclnn${OpName}.md`）。

op_host 层的文件构成：[docs/zh/install/dir_structure.md:L39-L51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L39-L51)。三个关键文件：

- `${op_name}_def.cpp`——算子信息库，定义名称、输入输出、数据类型等（必选）；
- `${op_name}_infershape.cpp`——根据输入推导输出 shape，未配置则输出与输入同 shape（可选）；
- `${op_name}_tiling*.cpp`——Tiling 策略（将张量切分成小块并行计算），且文档特别强调「Tiling 实现文件名须包含 `_tiling` 标识才会被编译系统识别」；`config/${soc_version}` 下还有按芯片型号区分的二进制配置（json + ini）。

op_api 与 op_kernel 层：[docs/zh/install/dir_structure.md:L52-L65](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L52-L65)。op_api 下是 `aclnn_${op_name}.cpp/h` 接口实现与 `${op_name}.cpp/h` l0 接口；op_kernel 下是 Kernel 入口 `${op_name}.cpp`、实现头 `${op_name}.h`，以及可选的 `${op_name}_tiling_key.h`（标识不同切分方式）和 `${op_name}_tiling_data.h`（存储块大小、并行度等配置），还可以有按子场景（如 arch35 架构）划分的子目录。

tests 层：[docs/zh/install/dir_structure.md:L66-L69](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L66-L69)，即 `tests/ut` 单元测试目录。

对照三个真实算子验证范式：

- **flash_attention_score**（层级最全）：含 `op_api/aclnn_flash_attention_score.cpp`、`op_host/flash_attention_score_tiling.cpp`、`op_graph/flash_attention_score_proto.h`、`examples/test_aclnn_flash_attention_score.cpp`，外加可选的 `framework/npu_flash_attention_score_onnx_plugin.cpp`（ONNX 插件层）。
- **matmul_all_reduce**：`op_api` 下有 v1/v2/v3 及 quant/weight_quant 多组版本文件（如 [mc2/matmul_all_reduce/op_api/aclnn_matmul_all_reduce_v3.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/matmul_all_reduce/op_api/aclnn_matmul_all_reduce_v3.cpp)），展示了「一个算子目录承载多版本接口」的组织方式。
- **moe_token_permute**：只有 op_host / op_kernel / tests / examples，**没有** op_api 与 op_graph——但它的 docs 下有 `aclnnMoeTokenPermute.md`，README 也提到调用 `aclnnMoeTokenPermute` 时框架内部会转调用 `aclnnMoeInitRoutingV2`（见 [moe/moe_token_permute/README.md:L108](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_token_permute/README.md#L108)）。这正是文档所说「缺 op_api 目录 → 调用了其他算子的 op_api 实现」的活例子：它的 aclnn 入口在 `moe/moe_init_routing_v2` 的 op_api 里。

#### 4.2.4 代码实践

**实践目标**：对照 dir_structure.md，实地核对三个算子目录的层级差异，并解释「为什么缺层」。

**操作步骤**：

1. 依次执行：

   ```bash
   ls attention/flash_attention_score
   ls moe/moe_token_permute
   ls mc2/matmul_all_reduce
   ```

2. 为每个算子画一张「层 × 有无」的勾选表（op_host / op_api / op_kernel / op_graph / tests / examples / docs / framework）。
3. 对缺失的层，回到 [docs/zh/install/dir_structure.md:L5-L11](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/dir_structure.md#L5-L11) 的四条规则给出解释假设。
4. 用算子自己的 README 验证假设：例如打开 `moe/moe_token_permute/README.md` 搜索 `aclnnMoeInitRoutingV2`，确认它的 aclnn 调用确实转发到了 moe_init_routing_v2 算子。

**需要观察的现象**：

- flash_attention_score 与 matmul_all_reduce 层级完整（含 op_api + op_graph），可直接 aclnn 调用；
- moe_token_permute 缺 op_api 与 op_graph，但 README 仍声称支持 `aclnnMoeTokenPermute`——矛盾点就是学习点；
- matmul_all_reduce 的 op_api 下同名文件有多个版本后缀（v2/v3/quant…）。

**预期结果**：得到如下结论表（可在自己的笔记中复现）：

| 层 | flash_attention_score | moe_token_permute | matmul_all_reduce |
|---|---|---|---|
| op_host | ✓ | ✓ | ✓ |
| op_api | ✓ | ✗（转发 moe_init_routing_v2） | ✓（多版本） |
| op_kernel | ✓ | ✓ | ✓ |
| op_graph | ✓（仅 proto） | ✗ | ✓ |
| tests / examples | ✓ | ✓ | ✓ |
| framework（可选第六层） | ✓（ONNX 插件） | ✗ | ✗ |

本实践为纯源码阅读型，无需 NPU 环境，所有命令均可本地验证。

#### 4.2.5 小练习与答案

**练习 1**：某算子目录下只有 `op_host`、`tests` 和 README，既没有 op_api 也没有 op_kernel。给出两种可能的解释。

**答案**：(1) 该算子复用了其他算子的 op_host/op_kernel 实现（调用逻辑可看其 op_api 或 op_graph 源码——但此处也没有，则更可能是第二种）；(2) Kernel 暂无 Ascend C 实现，属于未完成交付，社区欢迎参考 CONTRIBUTING.md 补充贡献。

**练习 2**：为什么 `op_host` 下的 Tiling 文件名必须包含 `_tiling` 标识？

**答案**：编译系统按文件名模式识别哪些源文件参与编译（见 dir_structure.md 第 47 行说明）。这是「约定优于配置」的工程手法：新增子场景 tiling（如 `_tiling_arch35.cpp`）无需改构建脚本即可被自动纳入。

**练习 3**：`attention/flash_attention_score/op_graph` 下只有 `flash_attention_score_proto.h`，没有文档模板中提到的 `graph_infer.cpp` 和 `fusion_pass`，这说明什么？

**答案**：op_graph 内部各文件也是可选的：该算子只需要 proto 原型供图引擎识别，数据类型推导等能力由其他机制承担。这与目录级「缺层」逻辑一致——粒度更细一层，同样是「按需交付」。

## 5. 综合实践

把本讲知识串成一张可复用的「算子速查地图」：

1. 从 `docs/zh/op_list.md`（算子清单，见上一讲）中任选一个你业务相关的算子。
2. 用 `ls` 列出它的目录层级，套用 4.2 的勾选表。
3. 对每一层，打开一个代表性文件写一句话注释（例如 op_host 下打开 `*_def.cpp`，记下它定义了几个输入、几个输出、几个属性）。
4. 判断该算子的调用路径：有 op_api → 可 Eager 调用，去 `examples/test_aclnn_*.cpp` 找样例；只有 op_graph → 走图模式；两者都缺 → 查 README 是否转发到其他算子。
5. 最终产出：一页 markdown 笔记，包含该算子的层级表 + 调用路径结论。这份笔记就是后续单元精读该算子时的导航图。

## 6. 本讲小结

- 仓库顶层目录分四类：算子域（attention/moe/mc2/ffn/gmm/mhc/mamba/posembedding）、公共（common/3rdparty）、工程设施（scripts/cmake/tests/docs/torch_extension）、用户扩展（examples/experimental）。
- 单个算子遵循「五层范式」：op_host（注册/shape/tiling）、op_api（aclnn 接口）、op_kernel（Device 核函数）、op_graph（图模式）、tests/examples（验证与示例），另有可选的 framework（ONNX 插件）层。
- 缺层有明确语义：缺 op_api = 不支持 aclnn 调用；缺 op_graph = 不支持图模式；缺 op_host/op_kernel 常意味着复用了其他算子的实现。
- 命名约定：目录与文件小写下划线，接口文档大驼峰（`aclnn${OpName}.md`）；tiling 文件名必须含 `_tiling` 才会被编译。
- 一个算子目录可以承载多版本接口（如 matmul_all_reduce 的 v1~v3 与 quant 变体）。

## 7. 下一步学习建议

目录地图建好后，下一步是让算子「跑起来」：先学 u1-l3 完成环境准备与源码获取，再学 u1-l4 掌握 build.sh 构建体系。之后进入第二单元，以 `examples/add_example` 教学算子为样本逐层精读五层范式中的每一层源码。建议提前浏览 `examples/add_example/README.md` 和 `docs/zh/install/build.md`，带着「这个目录每一层怎么被编译」的问题进入下一讲。
