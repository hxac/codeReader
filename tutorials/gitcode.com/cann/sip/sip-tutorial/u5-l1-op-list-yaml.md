# op_list.yaml：算子编译清单与 Operation-Kernel 映射

> **版本说明**：本讲义按 HEAD `8ab9fce` 撰写。该提交相对上一版仅对 `scripts/` 下三个 Python 脚本做了 ruff 格式化（统一引号风格、`exit(1)` 改为 `sys.exit(1)`），函数名与解析逻辑均未变化；文中引用的 `scripts/compile_ascendc.py`、`scripts/build_util.py` 行号均已按新版本刷新。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `configs/op_list.yaml` 的 **Operation → Kernel → 芯片架构** 三级映射结构，并能根据一份条目说出「哪个算子、哪个 kernel、编译到哪块芯片」。
2. 完整追踪一条传递链路：yaml 条目如何在 CMake 配置阶段被翻译成 `BUILD_` 开关变量，又如何在编译阶段驱动 `scripts/compile_ascendc.py` 调起 ccec 编译器产出 `.o` 与 `.json`，最后被 `scripts/build_util.py` 收集进交付目录。
3. 按官方文档规范，把一个新算子正确登记进 `op_list.yaml`，并理解**登记缺失的后果：算子根本不会被编译，运行期注册再完整也找不到 kernel**。

## 2. 前置知识

本讲站在 u1-l3（环境搭建与编译构建）和 u4-l1（算子目录结构）两讲的肩膀上，先回顾三个关键认知：

- **Host/Device 分层**：`core/` 是 Host 侧执行框架，`ops/` 是 Device 侧算子实现。`ops/` 下每个算子目录固定分三层：`*_operation.cpp`（注册与 shape 推导）、`tiling/`（数据切分，跑在 Host）、`op_kernel/`（AscendC 核函数，跑在 NPU）。
- **两层芯片开关**：`configs/build_config.json` 的 `targets` 决定「这次构建要照顾哪些芯片架构」，由 `scripts/build_util.py::get_build_target_list()` 消费（u1-l3 已精读）；`configs/op_list.yaml` 决定「每个 Operation 在哪些芯片上有哪些 kernel」。**本讲的主角是第二层**。
- **MKI 外部依赖**：`bash build.sh` 会自动 clone 并编译 `ascend-boost-comm`，把产物拷贝到 `3rdparty/mki`（见 [build.sh:L200-L214](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/build.sh#L200-L214)，该段逻辑先检查 `3rdparty/mki` 与编译器目录是否存在，不存在则 clone、构建、拷贝）。MKI 包里带着 `scripts/op_list_utils.py`、`cmake/op_build.cmake` 等构建件——**yaml 的「解析器」就住在那里，不在本仓库**。这一点决定了本讲的读码策略：仓库内看「调用点与消费者」，MKI 侧的逻辑只描述其在链路中的位置。

补充一个理解本讲的心智模型：`op_list.yaml` 之于 SiP，就像一张「点菜单」。CMake 配置阶段会照着菜单生成一堆 `BUILD_XXX` 形式的开关；编译阶段只有开着灯的窗口（开关为真）才会真正下厨（调 ccec 编译 kernel）。菜单上没写的菜，后厨根本不会备料。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [configs/op_list.yaml](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/configs/op_list.yaml) | 算子编译清单：Operation→Kernel→架构 三级映射（本讲主角） |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/CMakeLists.txt) | 顶层 CMake，定义 `OP_LIST_YAML_DIR` 指向 configs 目录 |
| [ops/CMakeLists.txt](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/CMakeLists.txt) | yaml 的直接消费者：调 MKI 的 `op_list_utils.py` 生成 `op_build.cmake` |
| [cmake/kernel_config.cmake](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/cmake/kernel_config.cmake) | 定义 `add_operation` / `add_kernel` 宏，按 `BUILD_` 开关挂编译命令 |
| [ops/base/conj/CMakeLists.txt](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/base/conj/CMakeLists.txt) | 算子侧调用宏的标准样本（登记的第二半） |
| [scripts/compile_ascendc.py](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py) | AscendC kernel 编译驱动：生成 ccec 命令、链接 fatbin、产出 meta json |
| [scripts/build_util.py](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/build_util.py) | 编译辅助：读 build_config、收集 `.o`+`.json` 并写 meta.ini |
| [docs/developing_a_simple_operator.md](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/docs/developing_a_simple_operator.md) | 官方「开发一个简单算子」教程，登记步骤的权威出处 |

## 4. 核心概念与源码讲解

### 4.1 yaml 结构：三级映射与四种典型形态

#### 4.1.1 概念说明

`op_list.yaml` 回答的是一个三段式问题：**哪个 Operation（Host 侧注册类）、在哪个芯片架构上、要编译哪个 Kernel（Device 侧核函数）**。它用 YAML 的两层嵌缩进表达三级信息：

```text
Operation 名（一级键，与 REG_OPERATION 注册的类名对应）
 └── Kernel/tactic 名（二级键，与 add_kernel 传入的 tactic 名对应）
      ├── ascend910b: true/false   （架构开关，三级）
      └── ascend950:  true/false
```

为什么需要这样一张清单？因为 SiP 的 kernel 是**按芯片架构分别编译**的：同一份 `op_kernel/*.cpp` 源码，针对 ascend910b 用 ccec 编译成 `dav-c220-vec` 目标，针对 ascend950 用 bisheng 编译成 `dav-c310` 目标。清单把「源码里有哪些 kernel」固化成一份可审查、可增删的构建数据，避免每次构建都全量扫描源码树。

#### 4.1.2 核心流程

解析这份 yaml 的职责在 MKI 包的 `op_list_utils.py`（外部依赖，仓库内不可见），它做两件事：

1. 若 `configs/op_list.yaml` 不存在，扫描 `ops/` 源码树自动生成一份；
2. 读取 yaml，为每个「Operation × tactic × soc = true」的三元组设置一个 CMake 变量：

\[ \text{BUILD\_}\langle Operation\rangle\_\langle Tactic\rangle\_\langle SoC\rangle = \text{TRUE} \]

这些变量写进生成的 `op_build.cmake`，被 `kernel_config.cmake` 的宏逐个判断。命名三元组与 yaml 键的**逐字对应**是整个机制的枢纽（4.2 会用源码验证）。

#### 4.1.3 源码精读

**形态一：最简条目**——一个 Operation 一个 kernel 一个架构。Conj 是全书反复使用的样本：

[configs/op_list.yaml:L73-L75](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/configs/op_list.yaml#L73-L75)——`ConjOperation` 下只挂一个 `ConjC64Kernel`，仅在 ascend910b 上编译。这三行就是 Conj 算子的全部「户口」。

**形态二：同 Operation 多 Kernel、多 dtype**——文件开头的 BLAS 区：

[configs/op_list.yaml:L1-L8](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/configs/op_list.yaml#L1-L8)——`AsumOperation` 只有 `SasumF32Kernel`（单精度），而 `CalOperation` 同时挂 `CscalC64Kernel`（复数）与 `SscalF32Kernel`（单精度）。Host 侧的 `GetBestKernel` 会按输入 dtype 在这些 kernel 中选择（呼应 u3-l3）。

**形态三：同 Operation 跨架构换 kernel 族**——FFT 的 C2R 家族：

[configs/op_list.yaml:L102-L122](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/configs/op_list.yaml#L102-L122)——`FftC2ROperation` 下，九个细分 kernel（按奇偶/阶数分类）全部只开 `ascend910b`，而 `FftC2RC64Kernel` 只开 `ascend950`。同一算子在两代芯片上用的是**完全不同的 kernel 集合**，这正是 u4-l4 讲过的多架构适配在编译清单上的投影。

**形态四：混合形态**——mul 同时按 dtype 与架构展开：

[configs/op_list.yaml:L210-L216](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/configs/op_list.yaml#L210-L216)——`MulC64Kernel`、`MulC32Kernel` 走 ascend910b，`MulArch35Kernel` 走 ascend950。注意 950 那行叫 `MulArch35Kernel` 而非 `MulC64Kernel`——**kernel 名必须与算子目录里 `add_kernel` 实际登记的 tactic 名一致**，是「户口」而非随意命名。

一个阅读时容易疑惑的细节：`FftStrideOperation` 在文件里出现了两次（[L188-L190](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/configs/op_list.yaml#L188-L190) 与 [L196-L198](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/configs/op_list.yaml#L196-L198)），内容完全相同。YAML 对重复键通常取后者，此处两块一致所以无实际影响，但它提醒我们：这份文件是手工维护的快照，出现笔误时不会报错，只会「悄悄少编或后块覆盖前块」。

#### 4.1.4 代码实践

1. **实践目标**：建立「看条目说出编译行为」的能力。
2. **操作步骤**：
   ```bash
   # 有多少个 Operation（一级键）
   grep -cE '^[A-Z][A-Za-z0-9]*Operation:' configs/op_list.yaml
   # 每个架构各承载多少 kernel
   grep -c 'ascend910b: true' configs/op_list.yaml
   grep -c 'ascend950: true'  configs/op_list.yaml
   # 反查：哪些算子已经适配了 950
   grep -nB 2 'ascend950: true' configs/op_list.yaml
   ```
3. **需要观察的现象**：950 的条目数量远少于 910b；支持 950 的算子（dft、mul、fft c2c/c2r arch35 等）恰好都是 u4-l4 提过有 arch35 分支或独立 kernel 的目录。
4. **预期结果**：910b 条目数是百级，950 条目数是个位到十位数；`grep -nB 2` 能直接给出「Operation → kernel → 架构」三元组。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`CalOperation` 下两个 kernel 的 `C64`/`F32` 后缀代表什么？
**答案**：数据类型编码——`C64` 表示 complex（float64 存储的复数对，实部虚部各 float），`F32` 表示单精度浮点。Host 侧 `GetBestKernel` 依据输入 dtype 选择其一。

**练习 2**：如果把 `MulArch35Kernel` 误写成 `MulArch35C64Kernel`（yaml 里），会发生什么？
**答案**：生成的开关变量变成 `BUILD_MulOperation_MulArch35C64Kernel_ascend950`，而 `ops/base/mul/CMakeLists.txt` 里 `add_kernel` 登记的 tactic 是 `MulArch35Kernel`，两者对不上，宏内的 `if` 判断为假——该 kernel 静默地不被编译，直到运行期找不到 kernel 才暴露。

### 4.2 编译清单联动：从 yaml 一行到 ccec 一条命令

#### 4.2.1 概念说明

yaml 是数据，本模块回答「数据如何变成编译动作」。整条链路分四站：

```text
① CMake 配置期：ops/CMakeLists.txt 调 MKI 的 op_list_utils
   读 configs/op_list.yaml，生成 op_build.cmake（内含 BUILD_ 开关变量）
② 宏展开期：kernel_config.cmake 的 add_operation / add_kernel
   检查 BUILD_<Op> 与 BUILD_<Op>_<Tactic>_<SoC>，决定是否挂目标
③ 编译期：add_kernel 挂的自定义命令调 scripts/compile_ascendc.py
   生成 ccec/bisheng 命令 → 产出 <tactic>.o + <tactic>.json
④ 收集期：build_util.py 把 op_kernels/<soc>/... 的产物归拢到
   obj/<soc>/... 并写 meta.ini，供最终打包
```

两层开关的叠加关系可以写成集合交：

\[ K_{\text{编译}} = \{\,(\text{op},\,\text{tactic},\,\text{soc}) \mid \text{yaml 中该三元组为 true}\,\} \cap \{\,\text{soc} \mid \text{build\_config.json 中 targets 为 true}\,\} \]

第一项在 ①② 站生效；第二项的**可验证落点**在收集期（`build_util.py` 只按 `get_build_target_list()` 返回的架构循环归拢），交集的另一部分判定发生在 MKI 生成的 `op_build.cmake` 内部——这部分源码不在本仓库，细节**待确认**，但不影响理解主线。

#### 4.2.2 核心流程

以 Conj 为例的完整时序：

1. 顶层 [CMakeLists.txt:L35](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/CMakeLists.txt#L35) 定义 `OP_LIST_YAML_DIR` 为仓库 `configs/` 目录。
2. `ops/CMakeLists.txt` 检查 yaml 是否存在：不存在则调 MKI 扫描器生成；存在则直接调 `build_cmake_options` 生成 `op_build.cmake` 并 include。
3. `op_build.cmake` 设置 `BUILD_ConjOperation` 与 `BUILD_ConjOperation_ConjC64Kernel_ascend910b`。
4. `ops/base/conj/CMakeLists.txt` 调 `add_operation(ConjOperation ...)` 与 `add_kernel(conj ascend910b vector ... ConjC64Kernel)`。
5. 宏内开关全真 → 挂自定义命令，调 `scripts/compile_ascendc.py --soc ascend910b --channel vector --kernel ConjC64Kernel ...`。
6. 脚本从源码提取 tiling key、按 soc×channel 查 arch、拼 ccec 命令、用 ld.lld 链接成 fatbin、写 meta json。
7. MKI 侧 `build_util.compile_ascendc_code` 把 `.o` 包装成可注册的 `.cpp`，汇入 `BINARY_SRC_LIST`。

#### 4.2.3 源码精读

**第①站：yaml 的两个消费入口。**

[ops/CMakeLists.txt:L27-L34](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/CMakeLists.txt#L27-L34)——若 `configs/op_list.yaml` 不存在，调 `${MKI_PACKAGE_DIR}/scripts/op_list_utils.py -s ops目录 -d configs目录` 自动扫描生成。**这解释了官方文档为什么允许「直接删除 op_list.yaml」来登记新算子**（见 4.3）：删掉后下次配置会重新扫描源码树生成新清单。

[ops/CMakeLists.txt:L36-L45](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/CMakeLists.txt#L36-L45)——把 yaml 路径与目标 `op_build.cmake` 路径传给 `op_list_utils.build_cmake_options()`，随后 `include` 生成的文件。开关变量就诞生在这一步。

**第②站：宏按开关放行。**

[cmake/kernel_config.cmake:L1-L8](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/cmake/kernel_config.cmake#L1-L8)——`add_operation` 宏：只有 `BUILD_${op}` 为真才创建 Host 侧对象库。**若 Operation 没登记进 yaml，连 `*_operation.cpp`（注册代码）都不会编译**——这就是「登记缺失 = 算子不存在」的第一层含义。

[cmake/kernel_config.cmake:L10-L17](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/cmake/kernel_config.cmake#L10-L17)——`add_kernel` 宏开头：`if (BUILD_${op_name}_${tac}_${soc})`。注意变量名的拼接方式——它与 yaml 三元组逐字对应，是第 4.1 节结论的直接证据。

[ops/base/conj/CMakeLists.txt:L17-L21](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/base/conj/CMakeLists.txt#L17-L21)——宏的真实调用样本：`add_operation(ConjOperation "${conj_src}")` 登记 Host 三件套（operation、kernel 启动封装、tiling）；`add_kernel(conj ascend910b vector conj/op_kernel/conj.cpp ConjC64Kernel)` 依次传入源码 kernel 名、soc、channel、核函数源文件、tactic 名——最后两个参数与 yaml 的 `ConjC64Kernel: ascend910b: true` 互为镜像。

**第③站：compile_ascendc.py 的五步流水。**

[cmake/kernel_config.cmake:L21-L40](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/cmake/kernel_config.cmake#L21-L40)——宏把 `--soc/--channel/--srcs/--dst/--kernel/--use_msdebug/--use_mssanitizer` 等参数组装成 `PYTHON_ARGS`，在 [L39](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/cmake/kernel_config.cmake#L39) 挂成自定义命令：`COMMAND python3 ${PROJECT_SOURCE_DIR}/scripts/compile_ascendc.py ${PYTHON_ARGS}`。输出目标为 `build/op_kernels/<soc>/<op>/<tactic>/<kernel>.o`。

[scripts/compile_ascendc.py:L20-L33](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L20-L33)——`parse_args()` 定义全部命令行参数，与上面 CMake 传参一一对应。

[scripts/compile_ascendc.py:L272-L281](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L272-L281)——`get_tiling_key_ids()` 用正则 `TILING_KEY_IS\((\d+)\)` 从核函数源码里抠出 tiling key 列表（没有则返回 `[0]`）。**一个 kernel 源文件可能编译多份**，每份对应一个 tiling key——这是 yaml 一个条目对应多个 `.o` 的原因。

[scripts/compile_ascendc.py:L284-L296](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L284-L296)——`get_arch()` 按 soc×channel 查表：ascend910b 的 vector 通道映射 `dav-c220-vec`，ascend950 映射 `dav-c310`。查不到返回 `"None"`，主流程随即失败退出。

[scripts/compile_ascendc.py:L337-L347](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L337-L347)——ascend910b 非 mix 分支：对每个 tiling key 生成一份目标文件名 `<dst>_<key>.o`，追加 `-D<kernel>=<kernel>_<key>` 与 `-DTILING_KEY_VAR=<key>` 宏定义后，调 `gen_compile_cmd_v220()`（[L76-L119](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L76-L119)，`-O3` 加一串 dav-c220 专属 llvm 选项）拼出 ccec 命令并执行。ascend950 走 [L407-L417](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L407-L417) 的 `gen_compile_cmd_c310()`（[L161-L191](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L161-L191)，换用 **bisheng** 编译器）。不支持的 soc 在 [L441-L444](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L441-L444) 报错并 `sys.exit(1)`。

[scripts/compile_ascendc.py:L446-L450](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L446-L450)——收尾两步：`gen_fatbin_cmd()`（[L194-L205](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L194-L205)）用 `ld.lld -m aicorelinux` 把所有 `.o` 静态链接成最终 fatbin；`gen_json()`（[L208-L252](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L208-L252)）写同名 `.json` 元数据，其中 [L235-L238](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/compile_ascendc.py#L235-L238) 按 channel 写 coreType/magic：vector 通道是 `VectorCore`/`AIV`/`RT_DEV_BINARY_MAGIC_ELF_AIVEC`——这些字段就是运行期 ACL 装载 kernel 时的「身份牌」。

**第④站：build_util.py 的收集与归拢。**

[cmake/kernel_config.cmake:L44-L56](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/cmake/kernel_config.cmake#L44-L56)——`.o` 产出后，另一条自定义命令在 MKI 脚本目录里调 `build_util.compile_ascendc_code()`（MKI 包内同名脚本，与本仓库 `scripts/build_util.py` 是「同源两份」的部署关系）把 `.o` 包装成 `build/obj/<soc>/<op>/<kernel>.cpp`，并汇入 `BINARY_SRC_LIST` 供 Host 库引用。

本仓库的 [scripts/build_util.py:L20-L52](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/build_util.py#L20-L52)——`get_build_target_list()`：读 `build_config.json`（可用环境变量 `BUILD_CONFIG_FILE` 覆盖路径），收集值为 `true` 的架构列表。这是两层开关交集中「本仓库可见」的那一半。

[scripts/build_util.py:L219-L252](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/build_util.py#L219-L252)——`copy_tbe_device_code()`：入口函数，从环境变量取 `CODE_ROOT/CACHE_DIR/ASDOPS_KERNEL_PATH`，读 `configs/tbe_tactic_json.ini`，在 [L242](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/build_util.py#L242) 调 `get_build_target_list()` 拿到架构清单后进入归拢流程。

[scripts/build_util.py:L114-L156](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/build_util.py#L114-L156)——`copy_ascendc_code()`：按 `<缓存目录>/op_kernels/<架构>/<Operation>/<tactic>/` 的目录约定遍历（正是第③站 compile_ascendc.py 的输出布局），对每个 `.json` 找配对的 `.o`，解析出 kernelList、magic、coreType 等信息，把文件拷进交付目录并登记 meta 表；缺 `.o` 配对或解析失败都直接 `sys.exit(1)`。

[scripts/build_util.py:L94-L110](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/scripts/build_util.py#L94-L110)——`write_meta()`：把收集到的每个 tactic 的 Object/OpName/KernelList/CompileInfo/CoreType/Magic 写成 `meta.ini`。**如果某个 kernel 没被编译（开关为假或漏登记），它自然不会出现在 meta.ini 里**——交付产物层面再次验证「不登记 = 不存在」。

#### 4.2.4 代码实践

1. **实践目标**：不改任何代码，纯靠阅读把「yaml 一行 → ccec 一条命令」的传递路径走通并记录成表。
2. **操作步骤**：
   1. 打开 [configs/op_list.yaml:L73-L75](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/configs/op_list.yaml#L73-L75)，记下三元组 `(ConjOperation, ConjC64Kernel, ascend910b)`；
   2. 按 4.2.3 的顺序依次打开八个源码点（ops/CMakeLists.txt 两处、kernel_config.cmake 三处、conj/CMakeLists.txt 一处、compile_ascendc.py 两处），在每处找到与该三元组相关的变量名或参数值；
   3. 手动拼出开关变量的完整名字，并与 `add_kernel` 宏内 `if` 语句的拼接结果比对；
   4. 拼出 compile_ascendc.py 将收到的完整命令行（对照 `parse_args` 的参数表）。
3. **需要观察的现象**：三元组的三个名字在链路中**一个字母都不改地**出现在：yaml 键 → `BUILD_` 变量 → `add_kernel` 实参 → `--kernel` 参数 → `.o`/`.json` 文件名。
4. **预期结果**：得到一张五列追踪表（环节 / 文件:行号 / 关键变量或参数 / Conj 的具体取值 / 下一环节）。若在构建机上（MKI 已就绪），还可在 build 目录 `find . -path '*ConjC64Kernel*'` 验证产物路径与 [kernel_config.cmake:L17-L18](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/cmake/kernel_config.cmake#L17-L18) 的拼法一致——**待本地验证**（本实践的主体部分只需读码，任何机器可完成）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_operation` 与 `add_kernel` 要分成两个开关（`BUILD_<Op>` 与 `BUILD_<Op>_<Tactic>_<SoC>`），而不是一个？
**答案**：因为 Host 侧与 Device 侧的编译粒度不同。`*_operation.cpp`、tiling 等是架构无关的 Host 代码，一个 Operation 编一份（`BUILD_<Op>`）；核函数要按架构×tactic 逐份编译（`BUILD_<Op>_<Tactic>_<SoC>`）。例如 `MulOperation` Host 代码一份，Device 侧却要为 910b 编 C64/C32 两份、为 950 编 Arch35 一份。

**练习 2**：`get_tiling_key_ids()` 从源码正则提取 tiling key，意味着什么？
**答案**：kernel 的「份数」不由 yaml 决定，而由核函数源码里 `TILING_KEY_IS(n)` 宏的出现次数决定。yaml 只登记到 tactic 粒度；同一 tactic 可按 tiling key 展开成多个 `.o`，最终由 `ld.lld` 链成一个 fatbin、由 `gen_json` 汇成一份 kernelList。

**练习 3**：`build_util.py`（本仓库）与 `compile_ascendc.py` 在链路中的位置有何不同？
**答案**：`compile_ascendc.py` 被本仓库 [kernel_config.cmake:L39](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/cmake/kernel_config.cmake#L39) 直接调用，是编译期的「生产者」；本仓库的 `build_util.py` 负责架构清单读取与产物归拢（`copy_ascendc_code`/`write_meta`），是构建后段的「打包员」，其 `copy_tbe_device_code` 依赖环境变量指路的缓存目录，直接调用点在仓库外（MKI 侧构建流程）。

### 4.3 新增算子登记：官方规范与两条路线

#### 4.3.1 概念说明

把一个新算子「接进编译体系」需要两侧配合：**yaml/CMake 侧登记**（本讲）与**源码侧实现**（u4 系列已讲）。官方教程 `docs/developing_a_simple_operator.md` 把登记列为「修改文件」步骤之一，并明确警告：不做这一步，新增算子的实现和接口**不会被真正编译进去**。登记有两条等价路线：

- **手改路线**：直接在 yaml 里追加条目——适合只加一两个算子，diff 清晰可审查；
- **重生成路线**：删掉 `op_list.yaml`，让配置期扫描器按 `ops/` 源码树重新生成——适合批量变更，但会重写整份文件，且生成规则由 MKI 侧控制。

#### 4.3.2 核心流程

手改路线的完整检查单（以假想的 Scale2 为例）：

```text
1. configs/op_list.yaml 追加：
   Scale2Operation:
       Scale2C64Kernel:
           ascend910b: true
2. ops/base/CMakeLists.txt 追加 add_subdirectory(scale2)
3. ops/base/scale2/CMakeLists.txt 内调用：
   add_operation(Scale2Operation "${scale2_src}")
   add_kernel(scale2 ascend910b vector scale2/op_kernel/scale2.cpp Scale2C64Kernel)
4. include/base_api.h 声明公开接口
→ 配置期生成 BUILD_Scale2Operation 与
  BUILD_Scale2Operation_Scale2C64Kernel_ascend910b，全链路放行
```

关键约束：yaml 里的 tactic 名（`Scale2C64Kernel`）、`add_kernel` 第五实参、以及核函数源码里的 kernel 名，三处必须一致；Operation 名则要与 `REG_OPERATION` 注册名一致。

#### 4.3.3 源码精读

[docs/developing_a_simple_operator.md:L100-L115](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/docs/developing_a_simple_operator.md#L100-L115)——官方教程「修改文件」一节原文：`op_list.yaml`「直接删除或者增加以下内容（这一操作非常重要，将新增算子信息加入列表，后续构建才会将新增算子的实现和接口真正编译进去）」，随后给出 Conj 的 yaml 片段与 `ops/base/CMakeLists.txt` 的 `add_subdirectory(conj)`。「删除重建」的合法性由 [ops/CMakeLists.txt:L27-L34](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/CMakeLists.txt#L27-L34) 的自动生成分支背书（4.2.3 已精读）。

[ops/CMakeLists.txt:L47-L52](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/CMakeLists.txt#L47-L52)——`add_subdirectory(base/fft/blas/filter/utils)`：新算子目录必须挂进对应模块的这条链，否则它的 `add_operation/add_kernel` 根本没机会执行——这是「只改 yaml 不改 CMake 也不生效」的原因。

[ops/CMakeLists.txt:L54-L55](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/CMakeLists.txt#L54-L55)——各模块对象库以 `--whole-archive` 方式打进 `asdsip_core`：保证「没人引用的注册代码也不被链接器丢弃」，注册宏 `REG_OPERATION` 的自注册才能在加载时生效。登记缺失的第二层后果由此闭合：没编译 → 没进 whole-archive → 运行期 Ops 单例查无此算子。

#### 4.3.4 代码实践

1. **实践目标**：为假想的 `Scale2` 算子完成 yaml 登记，并验证「yaml 只是数据，单独登记不产生编译目标」。
2. **操作步骤**：
   1. 建本地分支，在 `configs/op_list.yaml` 末尾追加：
      ```yaml
      Scale2Operation:
          Scale2C64Kernel:
              ascend910b: true
      ```
      （注意与上一条之间用空行分隔，缩进用 4 空格，与文件既有风格一致）；
   2. 不要创建任何 scale2 源码目录，直接在构建机上跑 `bash build.sh`（或至少 `cmake` 配置阶段）；
   3. 观察构建是否报错、`build` 目录中是否出现任何 `Scale2` 相关目标；
   4. 恢复方法：`git checkout configs/op_list.yaml`。
3. **需要观察的现象**：构建大概率**不报错**，也没有 Scale2 产物——因为没有任何 CMakeLists 调用 `add_kernel(... Scale2C64Kernel)`，`BUILD_Scale2Operation_Scale2C64Kernel_ascend910b` 开关空挂；这条实践反向证明了「yaml 与 add_kernel 互为镜像、缺一不可」。
4. **预期结果**：若配置期能看到 MKI 生成的 `op_build.cmake`（在 `3rdparty/mki/cmake/` 下），可 `grep Scale2` 找到被设置的开关变量，但 build 目录无对应 `.o`。**构建机上的具体现象待本地验证**；yaml 修改本身用 `python3 -c "import yaml,pprint;pprint.pprint(yaml.safe_load(open('configs/op_list.yaml'))['Scale2Operation'])"`（需 PyYAML）即可离线自查语法。

#### 4.3.5 小练习与答案

**练习 1**：新算子登记齐全（yaml + add_subdirectory + add_kernel）但 `build_config.json` 里 `ascend910b` 为 `false`，会发生什么？
**答案**：该架构不在 `get_build_target_list()` 返回清单中，910b 相关 kernel 不会进入最终产物；Host 侧架构无关代码仍会编译。两类开关是 AND 关系，都为真该 kernel 才落地。

**练习 2**：官方文档为什么允许「直接删除 op_list.yaml」这种看似危险的操作？
**答案**：因为 `ops/CMakeLists.txt` 的配置期分支会在文件缺失时调 MKI 扫描器按 `ops/` 源码树重新生成。yaml 本质是「可再生的快照」，删除即强制重建。代价是整份文件被重写、手工排序与分组丢失，所以少量增删时手改更稳妥。

**练习 3**：登记缺失为什么往往到运行期才暴露，而不是编译报错？
**答案**：整条链路都是「开关为假就静默跳过」：`add_operation`/`add_kernel` 的 `if` 不成立只是不挂目标，不产生错误；注册代码又因 whole-archive 机制对「已登记的那些算子」正常生效。直到用户调用新算子、Ops 单例按名查找失败（或查找成功但 kernel 二进制缺失）时才报错。

## 5. 综合实践

**任务：产出一份《Scale2 算子从 yaml 到 .o 的传递路径报告》。**

1. 在本地分支完成 4.3.4 的 yaml 登记，并补齐另一半：在草稿目录写一个最小 `ops/base/scale2/CMakeLists.txt`（内容仿照 [ops/base/conj/CMakeLists.txt:L17-L21](https://github.com/gitcode.com/cann/sip/blob/8ab9fcefc8637dc8f216996073fdb0d73f9db6e3/ops/base/conj/CMakeLists.txt#L17-L21)，把 Conj 全部替换为 Scale2，源文件路径可暂用 conj 的路径代替以验证机制）；
2. 按下表格式填写完整链路，每个环节给出 `文件:行号` 与 Scale2 的具体取值：

   | 环节 | 关键代码点 | Scale2 取值 |
   | --- | --- | --- |
   | yaml 登记 | op_list.yaml 追加条目 | `(Scale2Operation, Scale2C64Kernel, ascend910b)` |
   | 开关生成 | ops/CMakeLists.txt L36-L45（MKI 生成） | `BUILD_Scale2Operation_Scale2C64Kernel_ascend910b` |
   | Host 目标 | kernel_config.cmake add_operation | `Scale2Operation` 对象库 |
   | Device 命令 | kernel_config.cmake L21-L40 | `--soc ascend910b --kernel Scale2C64Kernel ...` |
   | 编译执行 | compile_ascendc.py L307-L450 | `dav-c220-vec`、`-O3`、tiling key 展开 |
   | 链接与元数据 | compile_ascendc.py L194-L252 | `Scale2C64Kernel.o` + `.json`（AIV magic） |
   | 收集归拢 | build_util.py L114-L156 | `op_kernels/ascend910b/Scale2Operation/...` → obj 目录 + meta.ini |

3. 有构建机的话，跑配置与编译，用 `find build -name '*Scale2*'` 与 `grep Scale2 <meta.ini 路径>` 验证表格每一行；没有构建机则标注「待本地验证」并保留读码结论；
4. 实验结束后还原所有改动（`git checkout configs/op_list.yaml`，删除草稿目录）。

这份报告是 u12-l2「从零开发一个全新算子」的直接预演——届时你只需把草稿换成真实的 tiling 与核函数实现。

## 6. 本讲小结

- `op_list.yaml` 是 Operation→Kernel→架构的三级编译清单，条目与 `add_kernel` 的实参、`REG_OPERATION` 的类名逐字对应，是「算子户口」而非普通文档。
- 解析器住在 MKI 包（`op_list_utils.py`）：配置期把 yaml 翻译成 `BUILD_<Op>_<Tactic>_<SoC>` 开关写入生成的 `op_build.cmake`；yaml 缺失时可按 `ops/` 源码树自动重生成。
- 编译期链路：`kernel_config.cmake` 的两个宏按开关放行 → 调 `scripts/compile_ascendc.py`（提 tiling key、查 arch、拼 ccec/bisheng 命令、ld.lld 链 fatbin、写 json）→ MKI 侧包装成 `.cpp` 汇入 Host 库。
- `scripts/build_util.py` 承担后段：`get_build_target_list()` 提供 build_config 的架构交集，`copy_ascendc_code()`/`write_meta()` 把 `.o`+`.json` 归拢进交付目录并写 meta.ini。
- 登记缺失的后果是「静默不存在」：Host 注册代码不编译（`add_operation` 开关为假）、Device kernel 不产出（`add_kernel` 开关为假）、meta.ini 无记录，最终在运行期查找算子时才暴露。
- 本次增量（`8ab9fce`）对两个脚本的 ruff 格式化（`sys.exit`、引号风格）不改变任何上述行为，仅行号漂移。

## 7. 下一步学习建议

本讲补齐了「构建系统深度」的最后一块地基：yaml 清单 → CMake 开关 → 编译驱动 → 产物收集。建议接下来：

1. **u5-l2（CMake 组织：Host 与 Device 编译分离）**：本讲只看了 `kernel_config.cmake` 的两个宏，下一讲展开 `host_config.cmake` 与顶层 CMakeLists，看清 `asdsip_core/asdsip_host/libasdsip` 三个库如何组装。
2. **u5-l3（run 包打包与发布）**：追踪本讲产物（obj 目录 + meta.ini）如何进入 makeself 自解压包。
3. **回头重读 u4-l4（多芯片架构适配）**：现在你能从 `op_list.yaml` 的 950 条目出发，解释 `arch35` 目录为何只在部分算子下存在。
4. 若准备动手开发，直接跳到 **u12-l1（按官方教程开发 Conj 算子）**，把本讲的登记检查单用在真实流程里。
