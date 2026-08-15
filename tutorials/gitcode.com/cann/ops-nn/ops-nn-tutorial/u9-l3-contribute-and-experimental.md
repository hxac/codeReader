# 贡献流程与 experimental 目录：调试并贡献自定义算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 ops-nn 社区贡献的完整六步流程（Issue → 需求评审 → PR → CI 门禁 → Committer 检视 → Maintainer 合入），以及每一步的产出与责任人是准。
2. 理解 `experimental/` 目录的定位：它是社区贡献算子的**统一落脚点**，同时容纳「生态最简算子」与「项目标准算子」两种交付形态。
3. 讲清楚 `build.sh --experimental` 这一个开关在构建系统里牵动的三条线：cmake 算子目录切换、`add_sources()` 算子的识别、torch_extension 打包范围切换。
4. 独立完成「在 experimental 下用 `--genop` 脚手架创建自定义算子目录 → 修改 → `--experimental` 编译 → 验证」的闭环。
5. 按 `CONTRIBUTING.md` 的交付件清单与合规检查项，为自己的贡献做一次上库前自查。

## 2. 前置知识

本讲是专家层「扩展开发与二次贡献」单元的一讲，默认你已完成 u9-l1（新建算子工程）。以下几个社区与工程概念先用通俗语言对齐：

- **Issue**：代码托管平台上的「议题工单」。贡献新算子前必须先建 `Requirement|需求建议` 类 Issue 说明背景、价值与设计方案，避免闭门造车后被拒。
- **CLA（Contributor License Agreement）**：贡献者许可协议，签署后你的代码才能按开源协议进入仓库。
- **SIG（Special Interest Group）**：特别兴趣小组。ops-nn 由 Ops-nn SIG 例会评审需求，维护者角色分三层：
  - **Committer**：检视代码、反馈意见；
  - **Maintainer**：最终审核，打 `/lgtm` 与 `/approve` 标签合入 PR。
- **CI 门禁**：PR 上库前的自动化检查流水线。在 ops-nn 中通过在 PR 评论 `compile` 指令触发，检查项包括代码编译、静态检查、UT 测试、冒烟测试。
- **生态最简算子**：为降低贡献门槛而定义的轻量交付形态——一个 `.cpp` 文件（kernel + torch 注册合一）加一个 Python 测试，通过 `torch.ops.ascend_ops_nn.<op_name>` 调用。它的载体正是 u5-l4 学过的 fast_kernel_launch 单文件模式。
- **项目标准算子**：仓库内置算子的完整形态，含 op_host（def/tiling）、op_kernel（含 tiling_data/tiling_key）、UT 测试，走 aclnn 两段式接口。

一句话概括两者区别：**生态最简算子面向 PyTorch 生态快速落地，项目标准算子面向 CANN 算子库全调用方式（aclnn/图模式）交付**。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [CONTRIBUTING.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md) | 贡献指南总纲：六步流程、交付件清单、CI 门禁与合入规则 |
| [docs/CONTRIBUTING_DOCS.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/CONTRIBUTING_DOCS.md) | 文档贡献指南：写作规范、提交规范、文档模板入口 |
| [experimental/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental) | 社区贡献算子落脚点，按算子大类分子目录（activation、matmul、norm 等） |
| [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) | `--experimental` 参数解析、`build_pytorch_extension` 算子扫描、`--genop` 脚手架入口 |
| [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake) | `add_category_subdirectory`：按 `ENABLE_EXPERIMENTAL` 切换算子大类目录 |
| [torch_extension/setup.py](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py) | 打包脚本：`TORCH_EXTENSION_EXPERIMENTAL` 环境变量切换收集范围 |
| [experimental/matmul/fast_hadamard_dynamic_quant/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant) | 生态最简算子的标准样本（四件套） |
| [experimental/activation/relu/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/activation/relu) | experimental 中「项目标准算子」形态的样本 |
| [scripts/opgen/opgen_standalone.py](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/scripts/opgen/opgen_standalone.py) | `--genop` 脚手架实现：复制 add_example 模板并整体改名 |

## 4. 核心概念与源码讲解

### 4.1 贡献流程总览：从 Issue 到 Maintainer 合入的六步

#### 4.1.1 概念说明

`CONTRIBUTING.md` 把「贡献新算子」定义成一条六步流水线。关键理解点是：**这条流程的仲裁权在社区（SIG 例会、Committer、Maintainer），而流程的每一步都有明确的工程产物要求**。代码写得再好，跳过 Issue 与需求评审直接提 PR，也可能因为方案未被接纳而被拒绝合入——这是开源协作与个人项目最大的差异。

#### 4.1.2 核心流程

```text
1. 创建 Issue 需求     → 产出：含背景/价值/设计方案的 Requirement Issue
2. 需求评审            → 产出：SIG 例会通过 + 分配贡献路径（experimental/${op_class}）
3. PR 提交             → 产出：符合交付件清单的代码 + README + 测试
4. CI 门禁             → 产出：编译/静态检查/UT/冒烟全绿（PR 评论 compile 触发）
5. Committer 检视      → 产出：检视意见全部闭环
6. Maintainer 合入     → 产出：/lgtm + /approve，代码进入 master
```

其中第 2 步有紧急通道：发邮件给 maintainer 申请临时 SIG 议题。

#### 4.1.3 源码精读

- [CONTRIBUTING.md:32-44](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md#L32-L44)：第 1、2 步原文。Issue 需包含**背景信息、价值/作用、设计方案**三要素；需求评审通过申报 SIG 议题、参加 Ops-nn SIG 例会完成。

- [CONTRIBUTING.md:57-59](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md#L57-L59)：**需求接纳**的落点——SIG 成员会为你分配算子分类路径（文档举的例子就是 `experimental/activation`），贡献算子必须提交到 `experimental` 对应分类目录。这回答了「为什么有 experimental 目录」：它是社区贡献的指定入口，与仓库内置算子物理隔离，方便统一检视与验收。

- [CONTRIBUTING.md:103-120](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md#L103-L120)：第 4-6 步原文。CI 门禁通过**在 PR 评论 `compile` 指令**触发，检查项为代码编译、静态检查、UT 测试、冒烟测试四类；codecheck 误报可提交 SIG 成员屏蔽。门禁绿了以后在 Issue 中 @ Committer，检视意见闭环后 Committer 标 `/lgtm`，Maintainer 标 `/approve` 合入。

- [README.md:15](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/README.md#L15)：仓库公告原文「2025/10 新增 experimental 目录，完善贡献指南，支持开发者调试并贡献自定义算子」，说明 experimental 目录是专门为贡献场景增设的机制。

#### 4.1.4 代码实践

1. **实践目标**：建立对贡献流程的「文档地图」认知，能快速定位每一步的权威说明。
2. **操作步骤**：
   - 打开 [CONTRIBUTING.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md)，把六步流程的章节标题抄成一张清单；
   - 点开文中链接的 [Issue 操作指南](https://gitcode.com/cann/community/blob/master/contributor/issue-operation.md) 与 [PR 操作指南](https://gitcode.com/cann/community/blob/master/contributor/pull_request_operation.md)，浏览 Issue 模板与 PR 模板要求的字段。
3. **需要观察的现象**：Issue 模板要求填写的字段与「背景/价值/设计方案」三要素如何对应；PR 模板要求关联 Issue。
4. **预期结果**：得到一张「步骤 → 文档位置 → 必填要素」对照表。此实践为文档阅读型，不涉及运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么「先提 Issue 再写代码」在 ops-nn 社区是硬建议而不是礼节？

**答案**：`CONTRIBUTING.md` 准备工作一节明确：涉及新增特性、新增接口、新增配置参数或修改代码流程（非简单 bug 修复）的改动，务必先通过 Issue 进行方案讨论，以避免代码被拒绝合入；不确定是否属于简单 bug 修复时也应提 Issue。此外新算子需求要经 SIG 例会评审并分配 `experimental/${op_class}` 路径，未经评审的 PR 缺少分配路径，无处安放。

**练习 2**：Committer 和 Maintainer 在合入流程中各自的权限动作是什么？

**答案**：Committer 负责检视并反馈意见，检视通过后标注 `/lgtm` 标签；Maintainer 做最终审核，确认无问题后标注 `/approve` 标签合入 PR。两个标签缺一不可。

**练习 3**：CI 门禁如何触发？包含哪四类检查？

**答案**：在 PR 下评论 `compile` 指令触发。检查项为：代码编译、静态检查（codecheck）、UT 测试、冒烟测试。

---

### 4.2 experimental 目录：贡献算子的落脚点与两种交付形态

#### 4.2.1 概念说明

`experimental/` 与仓库内置的 14 个算子大类目录平行，内部同样按大类组织（activation、conv、foreach、index、loss、matmul、norm、optim、pooling、quant、rnn、vfusion 等）。它同时收容两种形态：

1. **生态最简算子**：目录里只有「一个 `.cpp` + `tests/test_${op_name}.py` + `CMakeLists.txt` + `README.md`」四件套，CMakeLists 仅一行 `add_sources()`。调用入口是 `torch.ops.ascend_ops_nn.<op_name>`。当前样本有 matmul 下的 `fast_hadamard`、`fast_hadamard_quant`、`fast_hadamard_dynamic_quant` 和 norm 下的 `layernorm_stride`。
2. **项目标准算子**：完整 op_host/op_kernel/tests 结构，走标准 cmake 算子管线（与 u9-l1 的 genop 骨架同构），如 `experimental/activation/relu`。

**判别一个 experimental 算子属于哪种形态的方法：看它的 CMakeLists.txt 是否含 `add_sources(`**——这正是构建系统使用的判据（见 4.3）。

#### 4.2.2 核心流程

两类算子在构建系统里的分野：

```text
experimental/<大类>/<算子>/
├── CMakeLists.txt 含 add_sources(   → 生态最简算子
│     ├── 跳过标准算子 cmake 管线（不生成 opp 交付件）
│     └── 由 build_pytorch_extension() 收集，编入 torch_extension wheel
│           调用方式：torch.ops.ascend_ops_nn.<op_name>
└── CMakeLists.txt 不含 add_sources( → 项目标准算子
      ├── 走 add_op_subdirectory() 标准管线（op_host/op_kernel/opp 包）
      └── 调用方式：aclnn 两段式接口
```

#### 4.2.3 源码精读

- [CONTRIBUTING.md:63-73](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md#L63-L73)：指南给出的**生态最简算子交付件**目录树原文——`${op_name}.cpp`（Kernel 实现）、`tests/test_${op_name}.py`（测试）、`CMakeLists.txt`、`README.md`，共四件。

- [CONTRIBUTING.md:156-176](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md#L156-L176)：附录给出的**项目标准算子交付件**目录树——op_host 下 `_def.cpp` 与 `_tiling.cpp`，op_kernel 下入口 `.cpp`、实现 `.h`、`_tiling_data.h`、`_tiling_key.h`，外加 CMakeLists、README 与 tests/ut。这份树与 u9-l1 用 `--genop` 生成的骨架一致。

- [experimental/matmul/fast_hadamard_dynamic_quant/CMakeLists.txt:1](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/CMakeLists.txt#L1)：生态最简算子的 CMakeLists 全文只有一行 `add_sources()`，这就是它被构建系统识别为「PyTorch 扩展算子」的标记。

- [experimental/matmul/CMakeLists.txt:13-22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/CMakeLists.txt#L13-L22)：experimental 大类目录的 CMake 用 `file(GLOB)` 扫描全部子目录，存在 `CMakeLists.txt` 或 `op_host/CMakeLists.txt` 就 `add_subdirectory` 纳入——**新增算子目录零登记**，这是 u6-l3 讲过的仓库约定在 experimental 的延续。

- [experimental/activation/relu/op_host/relu_def.cpp:13-46](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/activation/relu/op_host/relu_def.cpp#L13-L46)：标准形态样本。`class Relu : public OpDef` 链式声明输入输出与 `AutoContiguous()`，末尾 `OP_ADD(Relu)` 注册——与 u3-l1 精读的 gelu_def 同一套机制，证明 experimental 标准算子与内置算子共用同一套 op_host 体系（配套的 [relu_infershape.cpp:21](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/activation/relu/op_host/relu_infershape.cpp#L21) `IMPL_OP_INFERSHAPE` 与 [relu_tiling.cpp:178](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/activation/relu/op_host/relu_tiling.cpp#L178) `IMPL_OP_OPTILING` 也都齐备）。

#### 4.2.4 代码实践

1. **实践目标**：学会用构建系统的判据（`add_sources(`）对 experimental 算子分类。
2. **操作步骤**：在仓库根目录执行下面的扫描命令（示例代码，非项目原有脚本）：

   ```bash
   grep -rE -l '^\s*add_sources\s*\(' experimental/*/*/CMakeLists.txt
   ```

   再对照列出 experimental 下没有命中、但存在 `op_host/` 子目录的算子。
3. **需要观察的现象**：命中 `add_sources(` 的目录恰好都是「单 .cpp + tests + README」四件套；未命中的目录展开后有 op_host/op_kernel/tests。
4. **预期结果**：命中清单应包含 `fast_hadamard`、`fast_hadamard_quant`、`fast_hadamard_dynamic_quant`、`layernorm_stride` 四个（与 `cmake/func.cmake`、`build.sh` 的扫描逻辑互为印证）。已通过仓库 grep 验证；具体输出以本地为准。

#### 4.2.5 小练习与答案

**练习 1**：生态最简算子与项目标准算子的调用入口分别是什么？

**答案**：生态最简算子通过 `torch.ops.ascend_ops_nn.<op_name>`（PyTorch torch.ops 机制，PrivateUse1 后端实现）调用；项目标准算子通过 aclnn 两段式接口（`aclnnXxxGetWorkspaceSize` + `aclnnXxx`）调用，并支持 GE 图模式。

**练习 2**：如果我贡献的算子只有 Host 侧逻辑、想最快被 PyTorch 用户用上，应选哪种形态？代价是什么？

**答案**：选生态最简算子形态（四件套 + `add_sources()`）。代价是牺牲通用性：没有 aclnn 接口、不支持 GE 图模式与 CANN opp 算子库的完整调用方式，调用面被限制在 PyTorch 生态内（与 u5-l4 对 fast_kernel_launch 的取舍分析一致）。

---

### 4.3 --experimental 构建机制：一条开关如何切换整个编译目标

#### 4.3.1 概念说明

`build.sh --experimental` 不是一个「附加编译 experimental」的开关，而是一个**目标切换**开关：它让本次构建**只面向 experimental 目录**。它同时牵动三条线：

1. **cmake 算子目录切换**：`ENABLE_EXPERIMENTAL=TRUE` 后，算子大类从 `<大类>/` 切到 `experimental/<大类>/` 查找；
2. **生态最简算子识别**：CMakeLists 含 `add_sources(` 的算子被标准管线跳过，交给 torch_extension 构建路径；
3. **torch_extension 打包范围切换**：wheel 只收集 experimental 目录下的算子。

#### 4.3.2 核心流程

```text
bash build.sh --pkg --experimental --soc=... --ops=...
        │
        ├─ build.sh 解析: ENABLE_EXPERIMENTAL=TRUE 且 ENABLE_TORCH_EXTENSION=TRUE
        │
        ├─ [线1] -DENABLE_EXPERIMENTAL=TRUE 传给 cmake
        │     └─ cmake/func.cmake add_category_subdirectory():
        │          op_category_dir = experimental/${op_category}     ← 目录切换
        │          CMakeLists 含 add_sources( → 跳过标准管线           ← 线2
        │
        └─ [线3] build_pytorch_extension():
              扫描 experimental/*/*/CMakeLists.txt 中的 add_sources(
              → PE_OPS 列表 → build_torch_extension.sh → ascend_ops_nn wheel
```

#### 4.3.3 源码精读

- [build.sh:927](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L927)：参数解析处 `experimental) ENABLE_EXPERIMENTAL=TRUE; ENABLE_TORCH_EXTENSION=TRUE`——注意它**顺带打开了 torch_extension 构建**，这就是「生态最简算子靠 wheel 交付」在参数层的体现。使用说明见 [build.sh:171](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L171) 与示例命令 [build.sh:188](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L188)。

- [build.sh:1103](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1103)：`CMAKE_ARGS` 追加 `-DENABLE_EXPERIMENTAL=${ENABLE_EXPERIMENTAL}`，把开关传入 cmake。

- [cmake/func.cmake:403-435](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake#L403-L435)：核心宏 `add_category_subdirectory`。第 405-409 行按 `ENABLE_EXPERIMENTAL` 把 `op_category_dir` 从 `${CMAKE_CURRENT_SOURCE_DIR}/${op_category}` 切到 `.../experimental/${op_category}`（线 1）；第 423-427 行读取每个算子的 CMakeLists，内容**匹配不到 `add_sources\s*\(`** 才走 `add_op_subdirectory()` 标准管线，匹配到的自然被跳过（线 2）。另有一个细节：名为 `common` 的子目录会被赋予 `<大类>.common` 的算子名，experimental/matmul/common 这类公共代码目录就是这样被豁免的。

- [build.sh:1818-1853](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1818-L1853)：`build_pytorch_extension()` 双层循环扫描 `experimental/<大类>/<算子>/CMakeLists.txt`，用 `grep -qE "^\s*add_sources\s*\("` 命中则加入 `PE_OPS` 列表；若 `--ops` 指定的算子与 PE_OPS 有交集（或未指定 --ops），调用 `scripts/torch_extension/build_torch_extension.sh` 完成打包（线 3 的收集端）。

- [torch_extension/setup.py:45-46](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L45-L46) 与 [torch_extension/setup.py:148-152](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L148-L152)：wheel 打包脚本读环境变量 `TORCH_EXTENSION_EXPERIMENTAL`（由 [build.sh:1801-1806](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1801-L1806) 导出），为真时**只**收集 experimental 目录的算子、为假时**跳过** experimental——单向互斥，保证 experimental 算子绝不混入内置整包。[torch_extension/setup.py:169-179](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L169-L179) 显示 experimental 采用三级路径收集（`<cat>/<subcat>/<op>/torch_extension`）。

- [build.sh:1606-1614](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1606-L1614)：`--run_example` 默认用 `grep -v experimental` 把 experimental 算子的样例排除在外；仅当 `--experimental` 时放开。即运行样例与编译共用同一个开关，行为一致。

- [docs/zh/develop/aicore_develop_guide.md:407-425](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/aicore_develop_guide.md#L407-L425)：官方开发指南的编译命令说明——编译 experimental 目录下用户自定义算子，命令需增加 `--experimental` 参数，完整形态为 `bash build.sh --pkg --soc=${soc_version} --vendor_name=${vendor_name} --ops=${op_list} --experimental`。

#### 4.3.4 代码实践

1. **实践目标**：不实际编译，仅通过构建脚本「dry-run 式」阅读，验证你对三条线的理解。
2. **操作步骤**：
   - 在 [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) 中找到 `experimental` 的 getopts 分支，确认它设置了哪两个变量；
   - 在 [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake) 的 `add_category_subdirectory` 中找到目录切换行与 `add_sources` 匹配行；
   - 用 `bash build.sh --help` 查看 `--experimental` 的帮助文案。
3. **需要观察的现象**：帮助文案给出的示例命令（`bash build.sh --pkg --experimental --soc=ascend910b --ops=${experimental_op}`）与 fast_hadamard 系列 README 中的编译命令一致。
4. **预期结果**：能不看讲义复述三条线各自的代码位置。`--help` 输出以本地为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`--experimental` 与 `--ops` 同时使用时，构建目标如何确定？

**答案**：`--ops` 圈定算子名单；`--experimental` 决定这些算子从 `experimental/<大类>/` 而非 `<大类>/` 查找。torch_extension 一侧同理：`build_pytorch_extension()` 只在 `--ops` 与扫描出的 `PE_OPS`（experimental 中含 `add_sources()` 的算子）有交集、或未指定 `--ops` 时才构建扩展包。

**练习 2**：为什么 experimental 算子不会混进默认的 `cann_ops_nn` 整包 wheel？

**答案**：`torch_extension/setup.py` 用 `TORCH_EXTENSION_EXPERIMENTAL` 环境变量做单向互斥：为真时 `continue` 跳过所有非 experimental 分类，为假时 `continue` 跳过 experimental 分类，两个分支互斥，因此物理上不可能混合。

**练习 3**：一个 experimental 标准算子（无 `add_sources()`）用 `--pkg --experimental` 编译，产物装到哪里？生态最简算子呢？

**答案**：标准算子走 cmake 标准管线，产物是安装到 `${ASCEND_HOME_PATH}/opp/vendors` 的自定义算子 run 包（与 u1-l2 一致）；生态最简算子被标准管线跳过，产物是 `build_out/ascend_ops_nn-*.whl`，用 pip 安装。

---

### 4.4 生态最简算子精读：fast_hadamard_dynamic_quant 四件套

#### 4.4.1 概念说明

`experimental/matmul/fast_hadamard_dynamic_quant` 是生态最简算子的教科书样本：融合「快速哈达玛变换 + 逐行动态 int4 量化」，fp16 输入变换后量化打包为 int8 字节（每字节 2 个 int4），并输出每行一个 fp32 scale。它把 u5-l4 学过的「单文件四合一」模式（Schema 注册 + Meta + Ascend C kernel + PrivateUse1 实现）落成了真实的贡献算子。

#### 4.4.2 核心流程

Host 侧一次调用的路径：

```text
torch.ops.ascend_ops_nn.fast_hadamard_dynamic_quant(x, hadamard_n, out, row_scales)
  └─ FastHadamardDynamicQuantNpu()
       ├─ TORCH_CHECK 校验：设备/dtype/连续性/fullN 为 2 的幂且 ∈ [64,16384]/整除关系
       ├─ 计算 batch、logHadamardN、invSqrtHadamardN，blockDim = min(batch, AIV_NUM=40)
       └─ OpCommand::RunOpApi 容器内 <<<blockDim, nullptr, stream>>> 启动 kernel
```

量化规格（README 定义）：每行 scale 与量化码为

\[ scale = max\_abs \cdot \frac{1}{\sqrt{hadamard\_n}} \cdot \frac{1}{7}, \qquad q = \mathrm{clamp}\left(\mathrm{round}\left(\frac{transformed \cdot 7}{max\_abs}\right),\ -8,\ 7\right) \]

其中 \( transformed \) 是未归一化的分块哈达玛变换结果，\( max\_abs \) 是该行绝对值最大值。int4 的 4 bit 精度是固有上限，所以测试用余弦相似度（阈值 0.98）而非逐元素对账。

#### 4.4.3 源码精读

- [experimental/matmul/fast_hadamard_dynamic_quant/README.md:33-42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/README.md#L33-L42)：README 里的目录结构自述——四件套齐备，公共的 Hadamard tile helper 放在 `experimental/matmul/common/fast_hadamard/`（跨算子复用的公共代码目录，正是 4.3 提到的 `common` 豁免机制的使用者）。

- [experimental/matmul/fast_hadamard_dynamic_quant/fast_hadamard_dynamic_quant.cpp:281-325](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/fast_hadamard_dynamic_quant.cpp#L281-L325)：Host 入口 `FastHadamardDynamicQuantNpu`。前半段是一串 `TORCH_CHECK` 参数闸门（NPU 设备、fp16/int8/fp32 dtype、连续性、`fullN` 为 2 的幂且在 \[64,16384\]），后半段计算派发标量并把启动包进 [RunOpApi 容器（L318-323）](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/fast_hadamard_dynamic_quant.cpp#L318-L323)——与 u5-l4 的结论呼应：即使走 `<<<>>>` 快速下发，也要包在 `RunOpApi` 内保持与 TorchNPU 的 aclnn 时序一致。

- [experimental/matmul/fast_hadamard_dynamic_quant/fast_hadamard_dynamic_quant.cpp:269-275](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/fast_hadamard_dynamic_quant.cpp#L269-L275)：`FastHadamardDynamicQuantKernelLaunch` 用 `<<<blockDim, nullptr, stream>>>` 直接启动核函数——生态最简算子没有独立 tiling 回调，切分标量（batch、fullN、hadamardN 等）作为核函数实参直传，这正是「tiling 退化为就地函数调用」的形态。

- [experimental/matmul/fast_hadamard_dynamic_quant/fast_hadamard_dynamic_quant.cpp:327-336](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/fast_hadamard_dynamic_quant.cpp#L327-L336)：torch 注册两件套——`TORCH_LIBRARY_FRAGMENT` 声明 schema（`Tensor x, int hadamard_n, Tensor out, Tensor row_scales -> int`），`TORCH_LIBRARY_IMPL` 把 NPU 实现挂到 `PrivateUse1` 后端。install wheel 后 `import ascend_ops_nn` 即完成注册。

- [experimental/matmul/fast_hadamard_dynamic_quant/README.md:47-56](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/README.md#L47-L56)：编译安装命令原文——`bash build.sh --pkg --experimental --soc=ascend910b --ops=fast_hadamard,fast_hadamard_quant,fast_hadamard_dynamic_quant` 后 `pip install --no-deps --force-reinstall build_out/ascend_ops_nn-*.whl`。注意 `--no-deps`：避免 pip 顺带升级环境里已固定版本的 torch/TorchNPU。

- [experimental/matmul/fast_hadamard_dynamic_quant/tests/test_fast_hadamard_dynamic_quant.py:49-74](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/tests/test_fast_hadamard_dynamic_quant.py#L49-L74)：贡献要求的测试件样本。`run_case` 在 CPU 上构造哈达玛矩阵作参考实现，NPU 结果解包 int4、乘回每行 scale 后与参考做余弦相似度，断言 > 0.98；主循环覆盖 n=128/256/512/1024 四档规模。**参考实现与 kernel 用同一数学定义**（1/sqrt(n) 归一化），是 u7-l2 讲过的 golden 对账原则在生态算子上的直接应用。

#### 4.4.4 代码实践

1. **实践目标**：跑通一个生态最简算子的「编译 → 安装 → 测试」全链路，获得可复制的贡献调试手感。
2. **操作步骤**（需 NPU 环境，torch/TorchNPU 已按版本配套装好）：

   ```bash
   cd ${git_clone_path}
   bash build.sh --pkg --experimental --soc=ascend910b \
       --ops=fast_hadamard,fast_hadamard_quant,fast_hadamard_dynamic_quant
   pip install --no-deps --force-reinstall build_out/ascend_ops_nn-*.whl
   ASCEND_RT_VISIBLE_DEVICES=<free-id> \
       python3 experimental/matmul/fast_hadamard_dynamic_quant/tests/test_fast_hadamard_dynamic_quant.py
   ```

3. **需要观察的现象**：构建日志出现「Building torch_extension whl with experimental ops only」；测试输出逐行打印 `batch=8 n=...: cosine=...`，最终打印 `PASSED`。
4. **预期结果**：各档规模的 cosine 约 0.99 量级（README 说明这是 int4 动态量化的固有精度地板），全部断言通过。本讲写作环境无 NPU，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：该算子的 blockDim 如何确定？为什么上限取 40？

**答案**：`blockDim = min(batch, AIV_NUM)`，batch 为 0 时兜底为 1。`AIV_NUM = 40` 是源码注释标明的 Atlas A2（Ascend910B）矢量核数，行级并行度超过核数后起更多核也无法受益，直接截断。

**练习 2**：测试为什么用余弦相似度 0.98 而不是 `torch.allclose`？

**答案**：输出是 int4 打包量化结果，4 bit 表示能力是固有精度地板（源码注释：大 n 下约 0.99 余弦）。若做逐元素严格对账，量化误差会被误判为算子错误；余弦相似度衡量的是变换方向保持程度，与量化目标匹配。这也体现了贡献指南「精度要求」一条：不同算子要按其精度标准选择合适的验证方法。

**练习 3**：这个算子的公共代码为什么不放在自己目录里？

**答案**：Hadamard tile helper 与 int4 转换模板被 fast_hadamard 三个姊妹算子共用，放在 `experimental/matmul/common/fast_hadamard/` 集中维护；`common` 目录在 cmake 侧被赋予 `<大类>.common` 算子名参与依赖解析，且不带 `add_sources()`、不产生独立交付件。

---

### 4.5 贡献自查清单：交付件、精度、CI 门禁与文档规范

#### 4.5.1 概念说明

贡献被拒最常见的原因不是代码错误，而是**交付件不全**。`CONTRIBUTING.md` 把要求整理成三张清单：交付件要求表、合规检查表、提交规范；文档侧另有 `docs/CONTRIBUTING_DOCS.md` 的写作规范。把它们当成 PR 前的 checklist 逐项打勾，是最省社区双方时间的做法。

#### 4.5.2 核心流程

PR 提交前的自查顺序：

```text
① 交付件三选齐：Kernel 实现 + 测试文件 + README（必选）
② 精度达标：对照生态算子开源精度标准（opbase 仓 experimental_standard）
③ 合规检查：C++ 编程规范 / 编译通过 / Markdown 语法
④ 目录正确：experimental/${op_class}（按 SIG 分配）
⑤ 描述规范：PR 标题清晰、描述含更改内容与原因、关联 Issue、已签 CLA
```

#### 4.5.3 源码精读

- [CONTRIBUTING.md:82-99](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md#L82-L99)：生态算子的**交付件要求表**——代码交付件（Kernel 实现 + 测试文件，参考 fast_kernel_launch_example）、文档交付件（README 必选）、精度要求（对照 opbase 仓的生态算子开源精度标准）；**合规检查**三项（C++ 编程规范、编译通过、Markdown 语法）；**提交规范**（目录 `experimental/${op_class}`、PR 标题与描述规范）。

- [CONTRIBUTING.md:178-197](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CONTRIBUTING.md#L178-L197)：**项目标准算子**的 PR 上库要求——交付件增加 op_host Tiling 实现与算子 UT 测试文件（参考资料指向算子开发指南），合规检查增加「符合标准算子基础编程规范」一条。也就是说：贡献标准算子的验收面比生态最简算子宽得多（多了 tiling 与 UT）。

- [docs/CONTRIBUTING_DOCS.md:21-57](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/CONTRIBUTING_DOCS.md#L21-L57)：文档贡献四步（准备工作/文档修改/提交更改/发起 PR），要点：从 master 或指定 Tag 下载源码、图片放 docs 下 figures 目录、**原子化提交**（一次提交只做一件事）、提交信息含简短说明与关联 Issue、PR 标题用 `[Docs] xxx` 风格前缀。

- [docs/CONTRIBUTING_DOCS.md:59-88](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/CONTRIBUTING_DOCS.md#L59-L88)：写作规范——先读 CANN 统一写作规范；代码示例必须可运行并注明环境前提；中英文混排用全角标点；图片推荐 png、单张不超 10 MB；引用资源须合规。算子 README 与 aclnn API 文档的模板入口在 [docs/CONTRIBUTING_DOCS.md:97-102](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/CONTRIBUTING_DOCS.md#L97-L102)（wiki 上的两份模板）。

- 对照真实样本：[experimental/matmul/fast_hadamard_dynamic_quant/README.md:9-31](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/matmul/fast_hadamard_dynamic_quant/README.md#L9-L31) 展示了一份合格 README 的结构——支持的 AI 处理器表格、算子规格（含 torch.ops 接口签名与全部约束）、正是「README 必选 + 精度可验证」两项要求的落地。

#### 4.5.4 代码实践

1. **实践目标**：把贡献清单变成可执行的核对动作。
2. **操作步骤**：
   - 假设你要贡献 4.4 精读的 `fast_hadamard_dynamic_quant`（或你自己的算子），按下表逐项核对并记录证据：

   | 检查项 | 要求来源 | 证据（文件/行为） |
   | --- | --- | --- |
   | Kernel 实现 | CONTRIBUTING.md 交付件表 | `fast_hadamard_dynamic_quant.cpp` |
   | 测试文件 | 同上 | `tests/test_fast_hadamard_dynamic_quant.py` |
   | README | 同上（必选） | `README.md` 含支持芯片与规格 |
   | 精度标准 | opbase 精度标准文档 | 测试断言策略（余弦 0.98） |
   | 目录 | 提交规范 | `experimental/matmul/` |
   | 编译通过 | 合规检查 | `--pkg --experimental` 构建成功 |
   | Markdown 语法 | 合规检查 | 本地 markdown lint 或渲染预览 |

   - 再用 `git log --oneline -5` 观察本仓库近期提交标题的风格（动词前缀 + 主题，如 `perf(logit_grad): ...`），对照文档贡献指南的提交信息要求。
3. **需要观察的现象**：清单里每一项都能在仓库中找到现成证据；提交历史标题简短且带模块前缀。
4. **预期结果**：产出一份全绿的核对表，即 PR 描述的雏形。

#### 4.5.5 小练习与答案

**练习 1**：生态最简算子与项目标准算子在「交付件要求」上差在哪两项？

**答案**：标准算子额外要求 op_host 的 Tiling 实现和算子 UT 测试文件（参考算子开发指南）；生态最简算子只要求 Kernel 实现与测试文件（Python 测试即可，参考 fast_kernel_launch_example）。

**练习 2**：贡献的 README 里「支持的 AI 处理器」表格写错了芯片型号，会先被谁拦下？

**答案**：不一定会被自动拦截——CI 门禁查的是编译、静态检查、UT、冒烟，文档事实性错误主要靠 Committer 检视发现；也可能由文档纠错流程（`Documentation|文档反馈` Issue）事后修正。所以自查阶段核对 README 的准确性是贡献者自己的责任。

**练习 3**：文档类 PR 的提交信息应遵循什么格式？

**答案**：`docs/CONTRIBUTING_DOCS.md` 提交更改一节要求：简短说明（不超过 50 字符），必要时另起详细段说明修改原因，末尾关联 Issue（如 `关联的Issue: #123`）；PR 标题建议 `[Docs] 修复快速入门中的配置示例` 这类前缀风格，描述中用 Closes/Fixes 关联 Issue。

## 5. 综合实践

**任务：在 experimental 下从零创建一个自定义算子目录，调试通过，并完成贡献自查。**

前置：已完成 u9-l1（理解 `--genop` 脚手架与算子五层交付件）。以下在配套 NPU 环境执行。

1. **脚手架生成**。利用 `--genop` 支持多级路径的特性（[build.sh:811-842](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L811-L842) 的 `process_genop` 会把 `experimental/activation` 解析为「基路径 experimental + 大类 activation」；实际目录拼装见 [scripts/opgen/opgen_standalone.py:45-47](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/scripts/opgen/opgen_standalone.py#L45-L47) 的 `dest_dir = output_path/op_type/op_name`）：

   ```bash
   bash build.sh --genop=experimental/activation/my_silu
   ```

   预期生成 `experimental/activation/my_silu/`，内含 op_host（def/infershape/tiling）、op_kernel、tests、examples、CMakeLists（模板来自 add_example，文件名与内容已整体替换，见 [opgen_standalone.py:49-58](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/scripts/opgen/opgen_standalone.py#L49-L58) 的 copy→rename→replace 三步）。命令输出以本地为准（待本地验证）。

2. **改成你的算子**。仿照 u1-l4 的做法：在 kernel 实现里把 `AscendC::Add` 换成 SiLU 语义 \( y = x \cdot \frac{1}{1+e^{-x}} \)（可用 `AscendC::Exp` 与 `AscendC::Mul` 组合，或先按 Mul 验证链路再进阶），同步修改 def 中的算子名注释与 README。

3. **按 experimental 方式编译**：

   ```bash
   bash build.sh --pkg --experimental --soc=ascend910b --ops=my_silu -j16
   ```

   对照 4.3 的三条线自检：cmake 应到 `experimental/activation/` 下找 `my_silu`；你的 CMakeLists 不含 `add_sources(`，应走标准 opp 管线而非 torch_extension。

4. **安装并验证**：安装 run 包后，仿照 [experimental/activation/relu/examples/test_aclnn_relu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/experimental/activation/relu/examples/test_aclnn_relu.cpp) 写一个最小调用样例（或改造 genop 生成的 examples），对照 CPU 参考值验证输出。

5. **贡献自查**：按 4.5.4 的核对表逐项打勾；若走生态最简形态，则改为仿照 fast_hadamard_dynamic_quant 建四件套（CMakeLists 写 `add_sources()`，kernel 与 torch 注册合入单文件），用 `--experimental` 编出 wheel 并运行 Python 测试。

6. **流程走位（可选，不真提 PR）**：按 4.1 六步流程，为这个算子起草一份 Issue 内容（背景/价值/设计方案三要素）和 PR 描述（更改内容、原因、关联 Issue）。

## 6. 本讲小结

- ops-nn 贡献流程是六步流水线：创建 Issue → SIG 需求评审 → PR 提交 → CI 门禁（评论 `compile` 触发，查编译/静态检查/UT/冒烟）→ Committer 检视（`/lgtm`）→ Maintainer 合入（`/approve`）；涉及新特性必须先 Issue 讨论方案。
- `experimental/` 是社区贡献算子的指定落脚点（SIG 评审后分配 `experimental/${op_class}` 路径），与内置算子物理隔离。
- experimental 内有两种交付形态：**生态最简算子**（四件套 + CMakeLists 一行 `add_sources()`，走 torch.ops 调用）与**项目标准算子**（op_host/op_kernel/tests 全件，走 aclnn）；构建系统用「CMakeLists 是否含 `add_sources(`」做机器判据。
- `--experimental` 是目标切换开关而非增量开关：cmake 侧把算子大类目录切到 `experimental/<大类>`，torch_extension 侧只打包 experimental 算子，两条路径单向互斥、绝不混包。
- 生态最简算子样本 `fast_hadamard_dynamic_quant` 展示了单文件四合一模式（TORCH_CHECK 闸门 + `RunOpApi` 容器内 `<<<>>>` 启动 + `TORCH_LIBRARY` 注册）与按精度特性选择验证方法（int4 量化用余弦相似度而非逐元素对账）。
- PR 前自查三张清单：交付件（生态最简=实现+测试+README；标准算子另加 tiling 与 UT）、精度标准、合规检查（编程规范/编译/Markdown）。

## 7. 下一步学习建议

本讲是 u9 单元（扩展开发与二次贡献）的第三讲。建议：

1. **补齐 u9 单元剩余讲义**：u9-l4（算子跨平台迁移）讲解多芯片适配，是把自己算子推向更多 SOC 的必经一步。
2. **动手实践真实贡献**：从「帮助解决他人 Issue」或「文档纠错」这类低门槛场景入手（`CONTRIBUTING.md` 第四、五节），走一遍完整 PR 流程建立手感，再挑战新算子贡献。
3. **阅读社区文档**：[cann-community 仓](https://gitcode.com/cann/community) 的 C++ 编程规范、Issue/PR 操作指南与 Ops-nn SIG 页面，是贡献前值得通读的三份材料；opbase 仓的生态算子精度标准决定你的算子能否过验收。
4. **回看关联讲义**：贡献标准算子前的知识准备——u3（def/infershape）、u4（tiling）、u7（UT/ST 测试体系）；贡献生态最简算子前的知识准备——u5-l4（fast_kernel_launch 单文件模式）与 u2-l3（torch_extension 机制）。
