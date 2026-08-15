# 算子跨平台迁移：多芯片适配与样例迁移指南

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `--soc=ascend910b` 这个参数从 shell 到 cmake 再到 kernel 二进制的完整映射链路，理解 `soc_version`、`dav 架构号`、`archNN 目录标签` 三者的对应关系。
2. 掌握构建层判定「某个算子在某块芯片上要不要编、怎么编」的两条路：`*_def.cpp` 里的 `.AddConfig("<soc>")` 与 `op_host/config/<soc>/*_binary.json`。
3. 看懂 `arch35` 这类架构目录的组织方式：`SUPPORT_COMPUTE_UNIT` 与 `SUPPORT_TILING_DIR` 的一一对应，以及同一算子在不同代际芯片上 tiling/kernel 分目录隔离的工程手法。
4. 对照官方《算子跨平台迁移指导》，把「硬件能力变更表」翻译成源码级的适配动作，能独立完成一次小算子的跨芯片迁移。

本讲是第 9 单元（扩展开发与二次贡献）的收官篇，前承 u9-l1 的 `--genop` 脚手架：脚手架生成的是「单芯片可用」的工程，本讲解决的是「如何让这个工程长出第二条芯片支线」。

## 2. 前置知识

### 2.1 三套「芯片名字」必须分清

初学者最容易混淆的是：同一个芯片，在仓库里有三套写法。

| 写法 | 例子 | 出现位置 | 含义 |
| --- | --- | --- | --- |
| soc_version 短名 | `ascend910b`、`ascend950` | `build.sh --soc=`、`AddConfig()`、`config/` 目录名 | 产品型号短名，构建入口的「人话名字」 |
| soc_version 长名 | `ascend910b1`、`ascend950pr_9599` | kernel 编译脚本内部 | 具体硬件版本，短名的细化 |
| dav 架构号 | `2201`、`3510` | `SOC_TO_ARCH` 映射表、CANN 包 `dav_*` 目录 | 指令集架构代号，决定用哪套编译器后端 |

另外还有一个容易误认成「架构号」的东西：**`arch22` / `arch35` 这样的目录名**。它是仓库源码里人为约定的「架构代际标签」（arch 2.2 代、arch 3.5 代），与 dav 架构号 `2201` / `3510` 数值上同源，但**不是程序自动推导的**，而是算子作者在自己的 `CMakeLists.txt` 里手工声明的（4.3 节会看到）。

短名到长名的换算表在 [cmake/func.cmake:820-830](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake#L820-L830) 的 `map_compute_unit`：`ascend910b→ascend910b1`、`ascend310p→ascend310p1`、`ascend910_93→ascend910_9391`、`ascend950→ascend950pr_9599`、`ascend350→ascend350_355e`、`mc62→mc62cm12aa`。本讲不深入 kernel 编译脚本，记住「短名是入口语言、长名是编译语言」即可。

### 2.2 一算子多芯片的三道闸门（回顾）

u3-l1 与 u5-l2 已经建立过这个认知，这里只做复习对照：

1. **def 声明闸门**：`AICore().AddConfig("<soc>", aicoreConfig)` 决定这块芯片「是否允许」有这个算子。
2. **binary 配置闸门**：`op_host/config/<soc>/<op>_binary.json` 决定这块芯片上「哪些 dtype 槽位」被预编译成二进制。
3. **tiling key 闸门**：kernel 入口按 tiling key 分发模板实例，dtype 与架构分支都在这里落地。

跨平台迁移本质上就是：在新芯片上把这三道闸门逐一打开，并且保证打开后的行为与旧芯片语义一致。

### 2.3 为什么「能跑」不等于「迁移完成」

Atlas A2（ascend910b）与 Ascend 950（ascend950）不只是核数不同：搬运通路有增删（L1→GM 直写被删、UB2L1/L0C2UB 直连被加）、编程范式有更替（Membase → Regbase、新增 SIMT）、片上缓存变大（L0C 从 128KB 到 256KB）。一份只为 A2 写的 kernel 在 950 上可能「能编过、能跑对」，但性能反而下降——迁移指南把这类问题归为三类典型病（5.4 节展开）。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) | 构建入口：`--soc` 解析、`SOC_TO_ARCH` 映射表、按架构目录挑样例 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CMakeLists.txt) | 仓库顶层 cmake：`ASCEND_COMPUTE_UNIT` 缓存变量与全量 soc 清单 |
| [cmake/gen_ops_info.cmake](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/gen_ops_info.cmake) | 多芯片判定核心：`get_op_type_and_validate`、`check_op_supported`、二进制编译调度 |
| [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake) | 公共函数：`find_value_by_key`、`add_tiling_sources`、`add_modules_sources`、`map_compute_unit` |
| [examples/add_example/op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp) | 教学样例 def：一份 `aicoreConfig` 复用给三块芯片 |
| [examples/add_example/op_host/config/ascend910b/add_example_binary.json](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/config/ascend910b/add_example_binary.json) | 教学样例唯一一份自配置 binary json（逐 dtype 槽位） |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp) | 教学样例 tiling：靠平台信息自适应，天然跨芯片 |
| [activation/gelu/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/CMakeLists.txt) | 生产算子样本：`SUPPORT_COMPUTE_UNIT` / `SUPPORT_TILING_DIR` 一一对应 |
| [activation/gelu/op_host/gelu_def.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp) | 生产算子 def：只对 ascend950 开放 |
| [activation/gelu/op_host/arch35/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35) | 950 专用 tiling（Regbase 范式） |
| [activation/gelu/op_kernel/arch35/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35) | 950 专用 DAG 与数据结构 |
| [activation/gelu/op_host/config/ascend950/gelu_binary.json](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/config/ascend950/gelu_binary.json) | 生产算子自配置 binary json（3 个 dtype 槽位） |
| [docs/zh/develop/cross_platform_migration_guide.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md) | 官方迁移指导：硬件差异表、推荐步骤、FAQ |

## 4. 核心概念与源码讲解

### 4.1 soc → arch：build.sh 的多芯片映射层

#### 4.1.1 概念说明

`--soc=ascend910b` 进来之后，build.sh 要回答三个问题：

1. 这个字符串是不是合法芯片短名？（合法性校验）
2. 要把它变成什么 cmake 变量？（参数透传）
3. 需要按架构代际挑选哪些额外文件？（架构相关资源）

这三个问题的答案分布在 build.sh 的三张「表」里：`SUPPORT_COMPUTE_UNIT_SHORT`（合法短名清单）、`SOC_TO_ARCH`（短名 → dav 架构号）、以及 `build_example` 里按 arch 目录挑样例的三个 if 块。

#### 4.1.2 核心流程

```text
用户输入 --soc=ascend950
        │
        ▼
① 参数解析：COMPUTE_UNIT = "ascend950"
        │
        ▼
② 前缀匹配 SUPPORT_COMPUTE_UNIT_SHORT（按字符串长度从长到短排序后逐个尝试）
        │  命中 → COMPUTE_UNIT_SHORT = "ascend950"
        │  未命中 → 报错 "The soc [...] is not support." 并退出
        ▼
③ 透传给 cmake：CMAKE_ARGS += "-DASCEND_COMPUTE_UNIT=ascend950"
        │
        ▼
④ cmake 侧 ASCEND_COMPUTE_UNIT 缓存变量生效，
   驱动 gen_ops_info.cmake 的 foreach(compute_unit ...) 循环
        │
        ▼
⑤（可选 --simulator）SOC_TO_ARCH["ascend950"]="3510"
   → 仿真库路径 $ASCEND_HOME_PATH/<arch>-linux/simulator/dav_3510/lib
```

多芯片用逗号分隔（如 `--soc=ascend910b,ascend950`），第②步会循环去重，最终 `ASCEND_COMPUTE_UNIT` 是一个分号分隔的多值 cmake 变量。

#### 4.1.3 源码精读

**第一张表：合法短名清单 + 长短名排序**。[build.sh:14-19](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L14-L19) 声明了全部支持的芯片短名，并做了一次关键的预处理：按字符串长度**从长到短**排序。

```bash
SUPPORT_COMPUTE_UNIT_SHORT=("ascend031" "ascend035" "ascend310b" "ascend310p" "ascend910_93" "ascend950" ...)
declare -A SOC_TO_ARCH
SOC_TO_ARCH=(["ascend310b"]="3002" ["ascend310p"]="2002" ["ascend910_93"]="2201" ["ascend910b"]="2201"
            ["ascend950"]="3510" ["ascend350"]="3510" ["ascend910"]="1001" ["mc62"]="5102")
# 对SUPPORT_COMPUTE_UNIT_SHORT按字符串长度从长到短排序，避免前缀匹配时出错
```

排序注释解释了为什么：`--soc` 匹配用的是「包含」语义（见第二段代码），如果不排序，`ascend910` 会抢在 `ascend910_93` 前面命中，把 910_93 误判成 910。

**第二张表：`--soc` 的前缀匹配与 cmake 透传**。[build.sh:1119-1146](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1119-L1146) 是真正的解析逻辑：

```bash
IFS=',' read -ra COMPUTE_UNIT <<<"$COMPUTE_UNIT"
COMPUTE_UNIT_SHORT=""
for unit in "${COMPUTE_UNIT[@]}"; do
  for support_unit in "${SUPPORT_COMPUTE_UNIT_SHORT[@]}"; do
    lowercase_word=$(echo "$unit" | tr '[:upper:]' '[:lower:]')
    if [[ "$lowercase_word" == *"$support_unit"* ]]; then
      ...COMPUTE_UNIT_SHORT="$COMPUTE_UNIT_SHORT$support_unit;"
      break
    fi
  done
done
if [[ -z $COMPUTE_UNIT_SHORT ]]; then
  print_error "The soc [${COMPUTE_UNIT}] is not support."
fi
echo "COMPUTE_UNIT: ${COMPUTE_UNIT_SHORT}"
CMAKE_ARGS="$CMAKE_ARGS -DASCEND_COMPUTE_UNIT=$COMPUTE_UNIT_SHORT"
```

要点有三个：大小写不敏感（`tr '[:upper:]' '[:lower:]'`）；去重（`";${COMPUTE_UNIT_SHORT};" != *";${support_unit};"*`）；失败直接终止。这意味着 `--soc=Ascend910B` 和 `--soc=ascend910b` 等价，容错性来自「包含匹配 + 排序」这套组合拳。

**第三处：cmake 侧的接收与默认值**。[CMakeLists.txt:96-101](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/CMakeLists.txt#L96-L101)：

```cmake
set(ASCEND_COMPUTE_UNIT
    "ascend910b"
    CACHE STRING "soc that need to be compiled")
set(ASCEND_ALL_COMPUTE_UNIT
    "ascend310p;ascend910;ascend910b;ascend910_93;ascend950;ascend350;ascend031;ascend035;ascend310b;ascend910_55;mc62;kirinx90;kirin9030"
    CACHE STRING "all soc list")
```

不传 `--soc` 时默认只编 `ascend910b`——这是初学者「明明源码里有 950 分支却没编出来」的第一嫌疑人。`ASCEND_ALL_COMPUTE_UNIT` 是全量清单（比 build.sh 的合法短名表多了 `ascend910_55`、`kirinx90` 等，说明 cmake 层认知的芯片比 shell 层入口暴露的更多）。

**第四处：`SOC_TO_ARCH` 的唯一消费点**。这张表在 build.sh 里只被 `--simulator` 分支用到，[build.sh:1521-1537](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1521-L1537)：

```bash
if [[ "${SOC_TO_ARCH[${unit}]}x" == "x" ]]; then
  usage "run_example"; exit 1
fi
SIMULATOR_PATH="${ASCEND_HOME_PATH}/${ARCH_INFO}-linux/simulator/dav_${SOC_TO_ARCH[${unit}]}/lib"
...
ln -sf ${SIMULATOR_PATH}/libruntime_camodel.so ${BUILD_PATH}/simulator/libruntime.so
```

它把 `ascend950` 换算成 `dav_3510`，去 CANN 包里找仿真替身库（这正是 u8-l3 讲过的 camodel 机制）。虽然消费点只有这一处，但它是仓库里唯一一张公开的「短名 → dav 架构号」对照表，是理解 `archNN` 目录命名由来的钥匙：**ascend950/ascend350 → 3510 → arch35 一代；ascend910b/ascend910_93 → 2201 → arch22 一代；ascend310p → 2002 → arch20 一代**。

**第五处：按架构目录挑样例**。[build.sh:1614-1623](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1614-L1623) 展示了 `--run_example` 如何按芯片追加架构专属样例：

```bash
files=($(find ../ -path "*/${OP_NAME}/examples/${pattern}*.cpp" ...))
if [[ "$COMPUTE_UNIT" == "ascend950" || "$COMPUTE_UNIT" == "ascend350" ]]; then
  files+=($(find ../ -path "*/${OP_NAME}/examples/arch35/${pattern}*.cpp" ...))
fi
if [[ "$COMPUTE_UNIT" == "ascend910b" ]]; then
  files+=($(find ../ -path "*/${OP_NAME}/examples/arch22/${pattern}*.cpp" ...))
fi
if [[ "$COMPUTE_UNIT" == "ascend310p" ]]; then
  files+=($(find ../ -path "*/${OP_NAME}/examples/arch20/${pattern}*.cpp" ...))
fi
```

注意 `examples/arch35/`、`examples/arch22/`、`examples/arch20/` 是**样例目录的约定**，与 tiling/kernel 的架构目录是同一套命名习惯。仓库里有 190+ 个 `examples/arch35/` 样例文件（如 [activation/gelu_quant/examples/arch35/test_aclnn_gelu_quant.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu_quant/examples/arch35/test_aclnn_gelu_quant.cpp)），它们只在 `--soc` 为 950/350 时被挑进编译清单——这解释了一个常见困惑：**为什么 `--run_example` 在 910b 上找不到某个 950 专属样例**。

#### 4.1.4 代码实践

**实践目标**：不编译任何东西，只通过构建脚本的「打印与报错」验证 `--soc` 的解析行为，建立对映射链路的肌肉记忆。

**操作步骤**：

1. 在仓库根目录执行 `bash build.sh --help`，在输出里找到 `--soc supported prefixes:` 一行，抄下支持的短名清单。
2. 执行 `bash build.sh --pkg --soc=Ascend910B --ops=add_example -j8 2>&1 | head -30`，在前几行找到 `COMPUTE_UNIT:` 输出。该行由 `assemble_cmake_args` 在 cmake 初始化之前打印（[build.sh:1142](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1142)），看到即可 Ctrl-C 中断，不必等编译跑完。注意不要在这里混用 `--make_clean`——它会在参数解析阶段直接 `exit 0`，轮不到打印这行。
3. 执行 `bash build.sh --pkg --soc=ascend999 --ops=add_example 2>&1 | head -10`，观察报错信息后同样中断。
4. 执行 `grep -n "SOC_TO_ARCH" build.sh`，确认这张表只在 `--simulator` 分支被消费。

**需要观察的现象**：

- 第 2 步中，大写 `Ascend910B` 被归一化成小写 `ascend910b` 并出现在 `COMPUTE_UNIT: ascend910b` 一行。
- 第 3 步输出 `[ERROR] The soc [ascend999] is not support.` 并终止。
- 第 4 步 grep 只返回第 15、16、1521、1525 四行，证明映射表消费点唯一。

**预期结果**：三次轻量命令各自命中 `--soc` 解析的一个分支（归一化命中 / 未命中报错 / 映射表定位）。完整编译链路涉及真实 CANN 环境，**待本地验证**（在配套 910b 环境上重复第 2 步并放行编译即可确认）。

#### 4.1.5 小练习与答案

**练习 1**：`--soc=ascend910_93` 和 `--soc=ascend910` 为什么必须靠「按长度排序」才能正确区分？如果 `SUPPORT_COMPUTE_UNIT_SHORT` 不排序会发生什么？

**答案**：匹配用的是 `"$lowercase_word" == *"$support_unit"*` 的包含语义。`ascend910_93` 包含子串 `ascend910`，如果短名靠前，会先命中 `ascend910` 并 `break`，把 910_93 误判成 910，编出错误的芯片二进制。按长度从长到短排序保证最具体的短名优先匹配。

**练习 2**：`SOC_TO_ARCH` 表里 `ascend950` 和 `ascend350` 都映射到 `3510`，这说明这两个 soc_version 之间是什么关系？

**答案**：它们同属一个 dav 架构代号（3510，即 arch35 代），指令集架构相同，因此可以共用 `arch35` 目录下的 tiling 与 kernel 实现；差异只在产品形态/资源配置上。gelu 的 `SUPPORT_COMPUTE_UNIT "ascend950" "mc62"` 配 `SUPPORT_TILING_DIR "arch35" "arch35"` 也是同样的思路（mc62 映射 5102，但 tiling 目录仍按作者约定挂 arch35）。

**练习 3**：用户执行 `bash build.sh --run_example gelu eager --soc=ascend910b`，预期会发生什么？

**答案**：`build_example` 只会按 `examples/test_aclnn_*.cpp` 与 `examples/arch22/` 挑样例，而 gelu 的样例在 [activation/gelu/examples/test_aclnn_gelu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/examples/test_aclnn_gelu.cpp)（通用位置），能被找到并编译；但 gelu 的 def 只 `AddConfig("ascend950")`，910b 上没有已安装的 gelu 自定义算子包，链接/执行阶段会失败。这演示了「样例挑得到」与「算子编得出」是两条独立链路。

---

### 4.2 构建层判定：AddConfig 与 config/<soc>/*_binary.json 两条路

#### 4.2.1 概念说明

`--soc` 只是「我想编这块芯片」。真正决定「这个算子在这块芯片上编不编、怎么编」的，是 cmake 层的判定函数 `get_op_type_and_validate`。它遵循一条优先级链：

```text
op_host/config/<soc>/<op>_binary.json 存在？
 ├── 是 → 走「self config（自配置）」路：json 就是编译清单，逐条目产出预编译二进制
 └── 否 → def 文件里有 .AddConfig("<soc>") 吗？
      ├── 是 → 走「def config」路：构建系统从 ops info 自动生成 binary json
      └── 否 → 该算子在这块芯片上不编译（"not supported"）
```

注意还有一道前置短路：`op_kernel/` 目录不存在就完全不编（说明这是一个纯 Host 算子或只交付 tiling 的算子）。

#### 4.2.2 核心流程

```text
foreach compute_unit in ASCEND_COMPUTE_UNIT:        # 来自 --soc，可多个
  foreach OP_DIR in COMPILED_OP_DIRS:               # 所有收集到的算子目录
    get_op_type_and_validate(OP_DIR, compute_unit)
        │
        ├─ op_kernel/ 不存在？ → is_valid = FALSE，跳过
        ├─ config/<soc>/<op>_binary.json 存在？
        │     └─ 从 json 里 grep 出 op_type → "compile binary with self config"
        └─ 否则：
              ├─ 从目录名推 op_type
              └─ check_op_supported：grep def 文件 '.AddConfig("<soc>"'
                    ├─ 命中 → "compile binary with def config"
                    └─ 未命中 → "[INFO] On [<soc>], [<op>] not supported."
    is_valid → prepare_compile_from_config(...)  # 真正生成 opc 编译脚本
```

关键在于：**判定是逐芯片、逐算子独立进行的**。同一个算子可以在 910b 上走自配置、在 950 上走 def 配置——add_example 正是这种情况（config 目录下只有 ascend910b 一份 json）。

#### 4.2.3 源码精读

**判定主函数**。[cmake/gen_ops_info.cmake:69-127](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/gen_ops_info.cmake#L69-L127)：

```cmake
function(get_op_type_and_validate OP_DIR compute_unit op_name_var op_type_var is_valid_var)
  ...
  set(binary_json ${OP_DIR}/op_host/config/${compute_unit}/${op_name}_binary.json)

  if(NOT EXISTS "${OP_DIR}/op_kernel")
    message(STATUS "[INFO] The op_kernel folder does not exist, [${op_name}] not need to compile.")
    ...return()
  endif()

  if(EXISTS ${binary_json})
    get_op_type_from_binary_json("${binary_json}" op_type)
    message(STATUS "[INFO] On [${compute_unit}], [${op_name}] compile binary with self config.")
    ...
  else()
    get_op_type_from_op_name("${op_name}" op_type)
    ...
    check_op_supported("${op_name}" "${compute_unit}" check_op_supported_result)
    if(NOT check_op_supported_result)
      message(STATUS "[INFO] On [${compute_unit}], [${op_name}] not supported.")
      ...
    endif()
    message(STATUS "[INFO] On [${compute_unit}], [${op_name}] compile binary with def config.")
  endif()
```

三行 `message(STATUS ...)` 就是排查迁移问题时最重要的三个路标日志：`compile binary with self config`（走自配置）、`compile binary with def config`（走 def 配置）、`not supported`（闸门未开）。

**AddConfig 的 grep 判定**。[cmake/gen_ops_info.cmake:503-518](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/gen_ops_info.cmake#L503-L518)：

```cmake
function(check_op_supported OP_NAME COMPUTE_UNIT OP_SUPPORTED_COMPUTE_UNIT)
  set(cmd "find ${OP_DIR} -name ${OP_NAME}_def.cpp -exec grep '\.AddConfig(\\s*\"${COMPUTE_UNIT}\"' {} \;")
  execute_process(COMMAND bash -c "${cmd}" OUTPUT_VARIABLE op_supported_compute_unit)
```

这是本讲最「朴素也最颠覆直觉」的一段：构建系统对 `.AddConfig()` 的判定**不是解析 C++ 语义，而是直接 grep 文本**，匹配模式为 `.AddConfig(\s*"ascend950"`。推论是：如果你把 soc 名拼在变量里再传给 `AddConfig`，或者写成换行/宏的形式，grep 会失配，算子会被判为不支持。**写 def 文件时 soc 字符串必须是字面量。**

**add_example 的三芯片声明**。[examples/add_example/op_host/add_example_def.cpp:66-78](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L66-L78)：

```cpp
OpAICoreConfig aicoreConfig;
aicoreConfig
    .DynamicCompileStaticFlag(true)
    ...
    .ExtendCfgInfo("opFile.value", "add_example"); // 指定的kernel入口文件名
// 为不同SOC版本添加AI Core配置
this->AICore().AddConfig("ascend910b", aicoreConfig);   // Ascend 910B芯片配置
this->AICore().AddConfig("ascend910_93", aicoreConfig); // Ascend 910A芯片配置
this->AICore().AddConfig("ascend950", aicoreConfig);    // Ascend 950芯片配置
```

这是「一份配置复用多芯片」的最简形态：同一个 `aicoreConfig` 对象连挂三次。它的成立有前提——add_example 的 tiling 全部从平台信息取数（见 4.2.3 最后一段），不含硬编码的芯片假设。

**自配置 binary json 的结构**。[examples/add_example/op_host/config/ascend910b/add_example_binary.json:1-45](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/config/ascend910b/add_example_binary.json#L1-L45)（节选第一条）：

```json
{
  "op_type": "AddExample",
  "op_list": [
    {
      "bin_filename": "AddExample_a1532827238e1555db7b997c7bce2928",
      "inputs": [
        { "name": "x1", "index": 0, "dtype": "float32", "format": "ND",
          "paramType": "required", "shape": [-2], "format_match_mode": "FormatAgnostic" },
        ...
      ],
      "outputs": [ { "name": "y", ... "dtype": "float32" ... } ]
    },
    { "bin_filename": "AddExample_11132827238e1555db7b997c7bce2928", ... "dtype": "int32" ... }
  ]
}
```

逐字段读懂它：

- `op_type`：算子注册名，`get_op_type_from_binary_json`（[cmake/func.cmake:808-815](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake#L808-L815) 就是对它做 `grep -w op_type` 再剥引号）。
- `op_list`：**每个元素对应一个 dtype 槽位的独立预编译二进制**。add_example 在 910b 上有两个条目（float32 / int32），与 def 里 `DataType({ge::DT_FLOAT, ge::DT_INT32})` 两个槽位一一对应。
- `bin_filename`：产出的二进制名，哈希后缀区分不同 dtype。
- `shape: [-2]`：动态 shape 通配（承接 u1-l3 / u3-l3 的结论）。

**自配置与 def 配置在编译调度上的分叉**。[cmake/gen_ops_info.cmake:622-666](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/gen_ops_info.cmake#L622-L666) 的循环里，两条路最终都汇入同一个 `prepare_compile_from_config`，区别只在于 json 从哪来：

```cmake
foreach(compute_unit ${ASCEND_COMPUTE_UNIT})
  foreach(OP_DIR ${COMPILED_OP_DIRS})
    get_op_type_and_validate("${OP_DIR}" "${compute_unit}" op_name op_type is_valid)
    set(binary_json ${OP_DIR}/op_host/config/${compute_unit}/${op_name}_binary.json)
    ...
    prepare_compile_from_config(
      TARGET ascendc_bin_${compute_unit}_${op_name}
      ...
      BINARY_JSON ${binary_json}
      IMPL_DIR ${OP_DIR}/op_kernel
      CONFIG_DIR ${OP_DIR}/op_host/config
      COMPUTE_UNIT ${compute_unit})
```

而在 [cmake/gen_ops_info.cmake:353-368](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/gen_ops_info.cmake#L353-L368)：自配置路直接把源码里的 json 拷到构建目录；def 配置路则反过来，把 `build/binary/<soc>/gen/<op>_binary.json`（由 `gen_bin_scripts` 目标从 ops info 自动生成）拷进构建配置区。**「json 是输入」还是「json 是输出」是两条路的本质区别**。

**为什么 add_example 能一份 tiling 通吃三块芯片**。[examples/add_example/op_host/add_example_tiling.cpp:76-94](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L76-L94)：

```cpp
static ge::graphStatus GetPlatformInfo(gert::TilingContext* context, uint64_t& ubSize, int64_t& coreNum)
{
    fe::PlatFormInfos* platformInfoPtr = context->GetPlatformInfo();
    auto ascendcPlatform = platform_ascendc::PlatformAscendC(platformInfoPtr);
    coreNum = ascendcPlatform.GetCoreNumAiv();
    ascendcPlatform.GetCoreMemSize(platform_ascendc::CoreMemType::UB, ubSize);
```

核数与 UB 大小都是运行时从 `TilingContext` 拿的，不是宏或常量。950 的 32 核 256KB UB 与 910b 的 24 核会自动得到不同的切分结果——这就是「参数化跨芯片」与「分目录跨芯片」两种策略的分水岭，4.3 节看后者的样本。

#### 4.2.4 代码实践

**实践目标**：为 add_example 补一份 `ascend950` 的自配置 binary json，把该芯片从「def config 路」切到「self config 路」，并从构建日志里确认切换发生。

**操作步骤**：

1. 先确认现状：`ls examples/add_example/op_host/config/`，应只有 `ascend910b` 一个目录。
2. 复制配置：`mkdir -p examples/add_example/op_host/config/ascend950`，然后 `cp examples/add_example/op_host/config/ascend910b/add_example_binary.json examples/add_example/op_host/config/ascend950/`。
3. 在 950 配套环境编译：`bash build.sh --pkg --soc=ascend950 --ops=add_example -j16 2>&1 | tee /tmp/build950.log`。
4. 在日志里搜关键行：`grep -n "self config\|def config\|not supported" /tmp/build950.log`。
5. （对照实验）把新 json 临时移走重编一次，重复第 4 步，对比两条日志。

**需要观察的现象**：

- 第 4 步（有 json）出现 `On [ascend950], [add_example] compile binary with self config`。
- 第 5 步（无 json）出现 `On [ascend950], [add_example] compile binary with def config`。
- 两条路最终都在 `build/binary/ascend950/bin/` 下产出 kernel 二进制与配套 config。

**预期结果**：add_example 的 def 本就 `AddConfig` 了三块芯片（[add_example_def.cpp:76-78](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L76-L78)），所以两条路都能编过；差异只在「json 是输入还是输出」。本实践需要真实的 950 CANN 环境，**待本地验证**。若手头只有 910b 环境，可把实验中的 soc 全部换成 `ascend910b` 并反向操作（移走现有 json），观察 `self config` → `def config` 的切换，现象等价。

#### 4.2.5 小练习与答案

**练习 1**：某算子 `--soc=ascend950` 编译时日志打印 `[INFO] On [ascend950], [foo] not supported.`，最可能的两处原因是什么？

**答案**：一是 def 文件里没有 `.AddConfig("ascend950", ...)` 这一行字面量（或写法带变量/换行导致 grep `.AddConfig(\s*"ascend950"` 失配）；二是 `op_host/config/ascend950/foo_binary.json` 不存在且上一条不成立。另有一个前置可能：`op_kernel/` 目录不存在（此时日志是 `not need to compile` 而非 `not supported`）。

**练习 2**：为什么说「自配置 json 是输入、def 配置 json 是输出」？从 cmake 源码指出证据。

**答案**：[gen_ops_info.cmake:353-368](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/gen_ops_info.cmake#L353-L368) 中，`if(EXISTS ${CONFCMP_BINARY_JSON})` 分支调用 `binary_config_copy` 把**源码里的 json 拷进构建区**（json 是输入）；`else()` 分支用 `cp ${CMAKE_BINARY_DIR}/binary/.../gen/${op}_binary.json` 把**构建区由 `gen_bin_scripts` 生成的 json** 拷进配置区（json 是输出）。

**练习 3**：如果把 add_example 的 config json 从 `ascend910b/` 目录挪到 `ascend950/`（即 910b 没有 json、950 有），`--soc=ascend910b,ascend950` 一次编译会发生什么？

**答案**：判定逐芯片独立进行：910b 走 def config 路（def 里有 `AddConfig("ascend910b")`），950 走 self config 路（json 存在）。两块芯片都会编出二进制，日志会分别打印两种 config 提示。这正是 `ASCEND_COMPUTE_UNIT` 为多值时 `foreach(compute_unit ...)` 外层循环的意义。

---

### 4.3 arch35 架构目录：tiling 与 kernel 的代际隔离（gelu 样本）

#### 4.3.1 概念说明

当两代芯片的差异大到「参数化」覆盖不了（编程范式不同、数据结构不同），就要**按代际拆目录**：tiling 与 kernel 各自维护一个 `arch35/` 子目录，构建时按 `--soc` 只编对应的那份。生产算子 gelu 是仓库里最干净的样本——它**只**支持 ascend950/mc62，tiling 全部下沉 `arch35/`，是「一看就懂」的代际隔离形态。

对比之下，更常见的生产形态是「新旧共存」：`op_host/` 根下放老代际的 `*_tiling.cpp`，`arch35/` 下放新代际的，由 `SUPPORT_COMPUTE_UNIT` / `SUPPORT_TILING_DIR` 两个等长列表按下标配对。

#### 4.3.2 核心流程

```text
算子 CMakeLists.txt 声明：
  SUPPORT_COMPUTE_UNIT  "ascend950"   "mc62"        ← 键列表
  SUPPORT_TILING_DIR    "arch35"      "arch35"      ← 值列表（与键一一对应）
        │
        ▼
add_modules_sources(...) 解析出 MODULE_COMPUTE_UNIT / MODULE_TILING_DIR
        │
        ▼
find_value_by_key(键列表, 值列表, 当前 ASCEND_COMPUTE_UNIT) → tiling_dir
   在键列表里 FIND 当前芯片 → 取同下标的值
   （找不到 → 空串；两列表长度不等 → FATAL_ERROR）
        │
        ▼
add_tiling_sources(source_dir, tiling_dir)
   tiling_dir 为空 → 只编根目录 *_tiling*.cpp
   tiling_dir 非空 → 根目录与 arch 子目录的 *_tiling*.cpp 都纳入
```

注意最后一行的语义：**非空时是「叠加」不是「替换」**——根目录与架构目录的 tiling 文件会被一起编进 `ophost_nn` 的 tiling 目标。因此「纯 arch35 算子」（如 gelu）的根目录下干脆不放任何 `*_tiling.cpp`，只留 `gelu_def.cpp` 与 `gelu_infershape.cpp`，从物理上保证不冲突。

#### 4.3.3 源码精读

**gelu 的声明处**。[activation/gelu/CMakeLists.txt:12-15](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/CMakeLists.txt#L12-L15)：

```cmake
# 设置算子定义时支持的芯片类型
set(SUPPORT_COMPUTE_UNIT "ascend950" "mc62")
# 设置每种芯片类型对应的tiling文件目录，即采用op_host目录下哪个文件夹下的tiling文件编译
set(SUPPORT_TILING_DIR "arch35" "arch35")
add_modules_sources(HOSTNAME ${OPHOST_NAME} MODE PRIVATE DIR ${CMAKE_CURRENT_SOURCE_DIR} OPTYPE gelu
                    ACLNNTYPE aclnn_exclude COMPUTE_UNIT ${SUPPORT_COMPUTE_UNIT}
                    TILING_DIR ${SUPPORT_TILING_DIR} DISABLE_IN_OPP TRUE)
```

对照教学样例 [examples/add_example/op_host/CMakeLists.txt:12](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/CMakeLists.txt#L12)：

```cmake
add_modules_sources(HOSTNAME ${OPHOST_NAME} MODE PRIVATE DIR ${CMAKE_CURRENT_SOURCE_DIR} OPTYPE add_example ACLNNTYPE aclnn)
```

add_example 完全不传 `COMPUTE_UNIT` / `TILING_DIR`，tiling 走「根目录 + 空 tiling_dir」的默认路；gelu 显式声明两张等长表。**跨芯片迁移时，这条 `add_modules_sources` 语句往往是你最先要改的一行。**

**键值配对函数**。[cmake/func.cmake:443-457](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake#L443-L457)：

```cmake
function(find_value_by_key key_list value_list search_key result)
  list(LENGTH key_list key_list_length)
  list(LENGTH value_list value_list_length)
  if(NOT ${key_list_length} EQUAL ${value_list_length})
    message(FATAL_ERROR "key_list length is ..., value_list length is ..., not equal")
  endif()
  ...
  list(FIND key_list ${search_key} index)
  if(NOT ${index} EQUAL -1)
    list(GET value_list ${index} found_value)
  endif()
```

两张表长度不等直接 `FATAL_ERROR`——这是「一一对应」约束的强制执行点，也是迁移时最容易踩的编译期报错（新增芯片忘了同步加 tiling 目录）。

**tiling 源收集**。[cmake/func.cmake:546-547](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake#L546-L547) 调用、[cmake/func.cmake:459-480](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/cmake/func.cmake#L459-L480) 实现：

```cmake
  find_value_by_key("${MODULE_COMPUTE_UNIT}" "${MODULE_TILING_DIR}" "${ASCEND_COMPUTE_UNIT}" tiling_dir)
  add_tiling_sources("${SOURCE_DIR}" "${tiling_dir}" "${MODULE_DISABLE_IN_OPP}")
```

```cmake
function(add_tiling_sources source_dir tiling_dir disable_in_opp)
  ...
  if("${tiling_dir}" STREQUAL "")
    file(GLOB OPTILING_SRCS ${source_dir}/*_tiling*.cpp)
  else()
    file(GLOB OPTILING_SRCS ${source_dir}/*_tiling*.cpp ${source_dir}/${tiling_dir}/*_tiling*.cpp)
  endif()
```

两分支的差异一目了然：非空时多 glob 一个 `${source_dir}/${tiling_dir}/` 目录，且**保留**根目录的收集。

**arch35 tiling 的内容**。[activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp:85-127](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L85-L127) 的 `RunTiling()` 主流程（承接 u5-l3 的 DAG 概念）：

```cpp
auto tiling = tilingContext->GetTilingData<Ops::Base::EleBaseTilingData16B>();
ElewiseBaseTiling elewiseBaseTiling(tilingContext);
...
if (this->outputDtype == ge::DT_FLOAT16) {
    dType = TPL_FP16;
    baseTilingResult = elewiseBaseTiling.DoTiling<GeluOp::GeluDAG<half>::OpDag>(*tiling);
} else if (this->outputDtype == ge::DT_BF16) {
    ...
} else if (this->outputDtype == ge::DT_FLOAT) {
    ...
}
...
const uint64_t tilingKey = GET_TPL_TILING_KEY(1, dType);
tilingContext->SetTilingKey(tilingKey);
tilingContext->SetBlockDim(elewiseBaseTiling.GetBlockDim());
```

与 add_example 手写两级切分（u4-l1）不同，arch35 这份 tiling 把切分委托给公共框架 `ElewiseBaseTiling`，自己只做三件事：校验 dtype/shape、按 dtype 选 DAG 模板参数、用 `GET_TPL_TILING_KEY(1, dType)` 编码出「1 = 架构分支，dType = 类型分支」的双参数 tiling key。**第一个模板参数 `1` 就是留给架构代际的编码位**——kernel 侧同一份入口按它实例化 arch35 专用二进制。

**TilingParse 的两段注册**。[activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp:139-151](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L139-L151)：

```cpp
ge::graphStatus TilingPrepareForGelu(gert::TilingParseContext* context)
{
    auto compileInfoPtr = context->GetCompiledInfo<GeluCompileInfo>();
    ...
    auto ascendcPlatform = platform_ascendc::PlatformAscendC(platformInfoPtr);
    compileInfoPtr->coreNum = ascendcPlatform.GetCoreNumAiv();
    ascendcPlatform.GetCoreMemSize(platform_ascendc::CoreMemType::UB, compileInfoPtr->ubSize);
    return ge::GRAPH_SUCCESS;
}

IMPL_OP_OPTILING(Gelu).Tiling(Tiling4Gelu).TilingParse<GeluCompileInfo>(TilingPrepareForGelu);
```

`GeluCompileInfo`（定义在 [activation/gelu/op_host/arch35/gelu_tiling_arch35.h:19-22](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.h#L19-L22)，就 `coreNum` 与 `ubSize` 两个字段）在模型加载阶段把**当前芯片**的核数与 UB 固化进编译信息——这就是 u5-l3 提过的「TilingParse 把芯片规格提前烤熟」，是跨芯片自适应的另一半。

**kernel 侧的对应下沉**。[activation/gelu/op_kernel/arch35/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/arch35) 下只有 `gelu_dag.h` 与 `gelu_struct.h` 两个头文件，而 [activation/gelu/op_kernel/gelu_apt.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_kernel/gelu_apt.cpp) 留在根目录。arch35 tiling 通过相对路径 `#include "../op_kernel/arch35/gelu_dag.h"`（[gelu_tiling_arch35.cpp:16-17](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/arch35/gelu_tiling_arch35.cpp#L16-L17)）引用它们——**DAG 是 host/device 双侧共用契约**（u5-l3 的结论在迁移语境下的再现）：tiling 按它算切分，kernel 按它驱动执行，所以代际隔离必须两侧同步。

#### 4.3.4 代码实践

**实践目标**：不改任何逻辑，只通过「删一张表的一个条目」观察 FATAL_ERROR 与 tiling 收集范围的变化，理解一一对应约束的强制力。

**操作步骤**：

1. 打开 [activation/gelu/CMakeLists.txt:12-14](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/CMakeLists.txt#L12-L14)，把第 14 行临时改成 `set(SUPPORT_TILING_DIR "arch35")`（删掉第二个 `"arch35"`，制造两张表长度不等）。
2. 执行 `bash build.sh --pkg --soc=ascend950 --ops=gelu -j8 2>&1 | tee /tmp/gelu_bad.log | grep -i "fatal\|not equal" | head -3`。`find_value_by_key` 的 FATAL_ERROR 发生在 cmake 配置阶段，无需等编译跑完。读完报错后**立刻把 CMakeLists 改回原样**。
3. 恢复后执行 `bash build.sh --pkg --soc=ascend950 --ops=gelu -j16 2>&1 | tee /tmp/gelu950.log`，然后 `grep -i "self config\|def config" /tmp/gelu950.log`（或直接 `ls build/` 检查产物）。
4. 对照执行 `bash build.sh --pkg --soc=ascend910b --ops=gelu -j8 2>&1 | grep -i "not supported\|self config\|def config" | head -3`，观察 910b 上 gelu 的判定结果（同样只需看到判定日志即可中断）。

**需要观察的现象**：

- 第 2 步出现 `key_list length is 2, value_list length is 1, not equal` 一类的 FATAL_ERROR。
- 第 3 步 gelu 在 950 上正常编译，tiling 来自 `arch35/` 目录。
- 第 4 步 gelu 的 def 只有 `AddConfig("ascend950")` 且无 `config/ascend910b/` json，日志应打印 `not supported`。

**预期结果**：第 2、4 步现象可在任何环境复现（FATAL_ERROR 发生在 cmake 配置阶段，`not supported` 是 STATUS 日志）；第 3 步需要 950 配套 CANN 包，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`add_tiling_sources` 在 `tiling_dir` 非空时是「叠加收集」而不是「只收子目录」。这对「新旧代际共存」的算子意味着什么？gelu 又是如何规避根目录冲突的？

**答案**：意味着根目录的老代际 `*_tiling.cpp` 与子目录的新代际 `*_tiling.cpp` 会一起编进同一个 tiling 目标，靠**注册的算子名或 tiling key 区分**各自生效范围。gelu 是「纯 950 算子」，干脆在根目录不放任何 `*_tiling.cpp`（op_host 根下只有 `gelu_def.cpp` 与 `gelu_infershape.cpp`），从物理上避免了同名注册冲突。

**练习 2**：gelu 的 `SUPPORT_COMPUTE_UNIT` 里有 `mc62`，而 build.sh 的 `SOC_TO_ARCH` 里 `mc62` 映射 `5102` 而非 `3510`。这矛盾吗？

**答案**：不矛盾。`SOC_TO_ARCH` 只服务于 `--simulator` 的仿真库路径定位；`SUPPORT_TILING_DIR` 的配对是算子作者在 CMakeLists 里**手工声明**的约定，不做数值推导。mc62 与 ascend950 同属一个代际（都可用 arch35 的 Regbase 范式 tiling），作者据此把它配到 `arch35`，两张表各司其职。

**练习 3**：如果要把 gelu 迁移到一块假想的新芯片 `ascend999`（同属 arch35 代际），最少要改哪几处？

**答案**：至少三处：① [gelu_def.cpp:42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L42) 加一行 `this->AICore().AddConfig("ascend999", aicoreConfig);`；② `SUPPORT_COMPUTE_UNIT` 追加 `"ascend999"`，`SUPPORT_TILING_DIR` 同步追加 `"arch35"`（保持等长）；③ build.sh 的 `SUPPORT_COMPUTE_UNIT_SHORT` 清单里得有这个短名，否则 `--soc=ascend999` 在入口就被拒。（可选项：`config/ascend999/gelu_binary.json` 自配置，或交由 def 配置路自动生成。）

---

### 4.4 从 Atlas A2 到 Ascend 950：硬件差异驱动的源码适配

#### 4.4.1 概念说明

前三个模块讲的是**构建层**的多芯片机制（目录怎么组织、怎么被选中）。本模块讲**源码层**：当两代芯片硬件能力真的不同时，kernel 代码要改什么。官方《算子跨平台迁移指导》把这件事组织成「硬件能力变更表 → 推荐迁移步骤 → 分类样例 → FAQ」四段，本模块带你把那张表逐行翻译成源码动作。

核心心智模型：**迁移不是「换个编译目标重编一遍」，而是「按硬件 diff 重写受影响的执行路径」**。构建层的 `arch35/` 目录只是给这些重写提供了物理隔离的容器。

#### 4.4.2 核心流程

官方推荐的四步迁移法（[cross_platform_migration_guide.md:121-127](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L121-L127)）：

```text
① 确认算子涉及的计算单元（Cube/Vector）及其支持的数据类型在两平台是否有差异
        ▼
② 确认数据搬运单元（ND->NZ、GM<->Lx、集合通信等）是否有差异
        ▼
③ 按硬件能力变更点逐项对照修改
   （Vector 架构 / Cube 数据类型 / L1/L0/UB 大小 / CCU 通信）
        ▼
④ 参考算子迁移样例调整/补齐 Atlas A2 与 Ascend 950 的分支逻辑
```

硬件能力变更可归为四类，每类对应不同的源码动作：

| 硬件单元 | 变更 | 源码动作 |
| --- | --- | --- |
| 搬运单元 | 删 L1→GM、删 GM→L0A/L0B 直连；新增 UB2L1 / L0C2UB 直连、ND DMA 随路 ND→NZ | 重构 DataCopy 链路与事件同步；CV 融合算子可把「L0C 回 GM 再读 UB」改为直达 |
| 计算单元 | Vector 新增 Regbase 范式；Cube 不再支持 int4_t；不支持 4:2 稀疏 | int4 算子换 int8 并改量化解算；稀疏 kernel 改稠密 |
| 存储单元 | L0C 128KB→256KB，UB 256KB | 重评 tile 尺寸与 L1/L0/UB 配比，减少切 K/切块轮次 |
| 其他 | GM 同地址并行优化；新增 SIMT 单元 | 简化「错位规避冲突」分核；离散访存子流程改 SIMT |

#### 4.4.3 源码精读

**第一步的输入：代际规格对比**。[cross_platform_migration_guide.md:29-61](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L29-L61) 给出关键数字（节选）：

| 规格项 | Atlas A2 | Ascend 950 |
| --- | --- | --- |
| AICore 核数 | 24 | 32 |
| Cube 算力 | 353T/376T @BF16,FP16 | 426T@BF16,FP16 / 757T@FP8 / 1514T@MXFP4 |
| Vector 算力(FP16) | 23.5T | 54T |
| Memory 容量 | 64GB | 128GB |
| L0C / UB | L0C 128KB | L0C 256KB、UB 256KB |

（L0C 尺寸差异在 [第 142 行](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L140-L142) 的 Tile 尺寸小节明确写出。）这些数字直接决定第③步的 tiling 重评：A2 时代为省 L0C 而做的细粒度切 K，在 950 上可能反而是流水断点。

**Cube 类算子的两类典型动作**。[cross_platform_migration_guide.md:132-142](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L132-L142)：

> 迁移时可将Atlas A2上为「错位规避冲突」设计的分核策略简化为更规则的滑动窗口模板（如行组窗口+列向往返扫描）……
> Atlas A2上L0C大小为128KB，Ascend 950提升到256KB……迁移时可优先增大Tile块切分粒度或提高K方向单轮处理深度。

注意方法论约束：**「先以功能等价为目标保留原 tile 尺寸，再逐步放开分核约束」**——迁移的第一里程碑是跑对，不是跑快。

**Vector 类算子：SIMT 与 Regbase 的取舍**。SIMT 部分给出了 gather_v2 的选择依据（[第 154-156 行](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L154-L156)）：尾轴 ≤ 2048 走 SIMT 模板、> 2048 走 SIMD 模板。两种编程模型的代码形态对比（[第 162-190 行](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L162-L190)）：

```cpp
// SIMD: 使用队列机制管理数据缓冲（显式 UB 队列 + DataCopyPad 批量搬运）
TQueBind<QuePosition::VECIN, QuePosition::VECOUT, BUFFER_NUM> inQueue_;

// SIMT: 使用线程级并行，无需显式buffer管理（__gm__ 指针直接访问）
__simt_vf__ LAUNCH_BOUND(2048) void GatherSimt(...) {
    for (INDEX_SIZE_T index = Simt::GetThreadIdx();
         index < currentCoreElements;
         index += Simt::GetThreadNum()) { ... }
```

Regbase 部分则给出 Membase → MicroAPI 的特征对照表（[第 216-224 行](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L216-L224)）：数据载体从 `LocalTensor<T>` + 队列变成 `RegTensor<T>` 寄存器，掩码从函数参数变成 `MaskReg` 寄存器。仓库侧的活样本就是 gelu 的 arch35 kernel（u5-l3 精读过 `gelu_apt.cpp` 的 MicroAPI 七指令实现），以及迁移建议（[第 318-322 行](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L318-L322)）：

> 1. 适合Regbase的场景：需要精细控制寄存器分配、复杂掩码逻辑、Gather/Scatter访存模式
> 2. 保留Membase的场景：简单的连续数据搬运和计算、双缓冲流水线
> 3. 混合使用：可在同一算子中结合两种范式

**CV 融合类：新直连通路**。[cross_platform_migration_guide.md:326-341](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L326-L341) 给出三个新接口签名：`DataCopy(const LocalTensor<T>& dst, const LocalTensor<T>& src, const Nd2NzParams&)`（UB2L1）、`Fixpipe(const LocalTensor<T>& dst, const LocalTensor<U>& src, const FixpipeParamsC310<...>&)`（L0C2UB）、`CrossCoreSetFlag/WaitFlag` 新增 mode 3。这直接对应 gelu 这类算子不需要、但 matmul 融合算子必须动的路径。

**最容易踩的死锁坑：核间同步信号量匹配**。[cross_platform_migration_guide.md:365-376](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L365-L376)：

> 在Ascend 950上`CrossCoreWaitFlag`和`CrossCoreSetFlag`数量必须严格匹配……Atlas A2上算子与算子间若存在多余`CrossCoreSetFlag`信号量，HWTS会进行特殊处理清零计数器，Ascend 950系列为减少硬件开销，不再依赖该类兜底机制，要求单算子内核间同步信号量一一匹配，否则会出现必现卡死。

文档并列出三类被 A2 掩盖、在 950 上直接暴露的病灶：异常分支提前返回导致 Set/Wait 不配对、多 stage 复用同一 `flagId` 生命周期重叠、循环边界不对齐。**「在 A2 上能跑」不能作为同步正确性的证据。**

**迁移完成的验收：FAQ 三问**。[cross_platform_migration_guide.md:460-467](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L460-L467)：

> 若算子在Ascend 950上性能不升反降时，可优先排查：
> 1. 是否仍然使用Atlas A2的错位分核模板
> 2. 是否未开启CCU通信仍走AICPU
> 3. tiling是否沿用了Atlas A2的L1/L0/UB切分策略，导致Ascend 950更大的片上缓存未被充分利用

这三问正好对应 4.4.2 表格的三个动作项，是迁移后的自检清单。

**回看 add_example：为什么它不需要任何 arch35 目录**。它的 tiling 只依赖 `GetCoreNumAiv()` 与 `GetCoreMemSize(UB)`（[add_example_tiling.cpp:76-94](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L76-L94)），kernel 只用最基础的 `TQue`/`DataCopyPad`/`AscendC::Add`（u5-l1、u5-l2 精读），不触碰任何一代被删掉的通路、不依赖 Regbase/SIMT、不做核间同步。**元素级连续访存 + Membase 双缓冲**正落在迁移建议「保留 Membase 的场景」里。这就是它能用同一份 `aicoreConfig` 连挂三块芯片的底层原因——不是运气好，是问题域简单。

#### 4.4.4 代码实践

**实践目标**：做一次「纸面迁移评审」——用官方四步法与硬件 diff 表，评审 add_example 迁移到 950 的完整性，并对照仓库里真实的 arch35 样例验证你的判断。

**操作步骤**：

1. 通读 [cross_platform_migration_guide.md:63-119](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/cross_platform_migration_guide.md#L63-L119) 的「硬件能力变更引入适配点」全表，建一张三列清单：`变更项 | add_example 是否受影响 | 依据（源码行号）`。
2. 对每一行「受影响」给出源码证据；对每一行「不受影响」也给出证据（例如「未使用 CrossCoreSetFlag → 核间同步条目不受影响，依据 add_example.h 全文无此调用」）。
3. 挑一个真实的 arch35 样例做对照，例如 [activation/gelu_quant/examples/arch35/test_aclnn_gelu_quant.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu_quant/examples/arch35/test_aclnn_gelu_quant.cpp) 与同算子的通用样例（若存在），diff 两者差异，判断 arch35 版本是否用了 950 专属的 dtype（如 fp8/mxfp8）或更大的输入规模。
4. 用 `grep -rn "CrossCoreSetFlag" examples/add_example/` 与 `grep -rn "simt\|MicroAPI" examples/add_example/` 两条命令确认 add_example 干净。

**需要观察的现象**：

- 第 4 步两条 grep 均无命中（add_example 确实不涉及核间同步与新一代范式）。
- 第 3 步能观察到 arch35 样例与通用样例在输入构造上的差异（dtype 或 shape），或确认两者一致仅目录归属不同。
- 你在第 1-2 步产出的清单里，「不受影响」的证据全部能落到具体源码行，而不是凭感觉。

**预期结果**：得出结论「add_example 可零源码改动迁移 950，仅需 def 已有的 `AddConfig("ascend950")` + 构建层配置（自配置 json 或 def 配置路）」，并能指出**哪一类算子不能这么做**（用到 int4_t、4:2 稀疏、L1→GM 直写、错位分核模板、AICPU 集合通信的算子）。第 3、4 步 grep 与文件 diff 在任何环境可完成；涉及真实编译验证的部分**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：一个 Atlas A2 上的 matmul 融合算子迁移到 950 后「功能正确但性能反降」，按 FAQ 三问逐条说明排查方法。

**答案**：① 查分核模板：看 tiling/kernel 是否仍是 A2 的「错位规避冲突」策略（950 已支持 GM 同地址并行，可简化为规则滑动窗口），用 msprof op 看 MAC/MTE2 利用率（u8-l2 方法）；② 查通信路径：若算子含跨片 allreduce，确认 eager 模式下是否对 `DAV_3510` 架构设置了 `NNOPBASE_HCCL_SERVER_TYPE_CCU`，还是仍走 AICPU；③ 查 tiling 切分：确认是否沿用 A2 的 L1/L0/UB 预算，950 的 L0C 256KB 允许更大 tile 与更深切 K，可用 tiling UT（u7-l1）断言新切分值。

**练习 2**：迁移指南说 950 要求 `CrossCoreSetFlag` 与 `CrossCoreWaitFlag` 严格一一匹配，否则「必现卡死」。为什么这类问题在 A2 上可能长期潜伏？

**答案**：A2 的硬件调度器 HWTS 会对多余的 Set 做特殊处理清零计数器，等于硬件兜底掩盖了不配对；950 为省硬件开销移除了该兜底，逻辑错误直接暴露为阻塞超时或死锁。三类典型触发：异常分支提前 return 只 Set 不 Wait（或反之）、多 stage 复用同一 flagId 且生命周期重叠、循环内条件同步但迭代次数不一致。

**练习 3**：迁移指南给出的 Regbase/Membase 取舍建议中，「混合使用」指什么？gelu 的 arch35 实现属于哪一类？

**答案**：混合使用指同一算子内用 Regbase（MicroAPI/RegTensor/MaskReg）写核心计算逻辑、用 Membase（LocalTensor + TQue 队列）管理数据搬运。gelu 的 arch35 实现以 Regbase 的 DAG + MicroAPI 为主（`gelu_apt.cpp` 中 GeluCustom 用 7 条矢量指令实现），搬运与调度交由公共的 `ElementwiseSch16B` 调度器处理，整体偏 Regbase/公共框架路线，而非手工混排。

## 5. 综合实践

**任务：给 add_example 做一次完整的「910b → 950」迁移，并产出一份迁移评审报告。**

前置：需要一套与源码配套的 Ascend 950 CANN 环境（toolkit + `set_env.sh`）。若无 950 环境，可用 910b 环境完成第 1、2、5 步，第 3、4 步标注「待本地验证」。

1. **构建层开闸门**：确认 [add_example_def.cpp:76-78](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L76-L78) 已含 `AddConfig("ascend950", aicoreConfig)`（无需改动，写进报告说明为什么不用改：配置对象不含芯片特定假设）。
2. **binary 配置二选一**：
   - 方案 A（自配置）：按 4.2.4 的步骤，把 910b 的 json 复制为 `config/ascend950/add_example_binary.json`；
   - 方案 B（def 配置）：什么都不加，让构建系统从 ops info 自动生成。
   两种方案各编一次，用 `grep "self config\|def config"` 记录日志差异。
3. **编译与安装**：`bash build.sh --pkg --soc=ascend950 --ops=add_example -j16`，安装 `build_out/` 下的 run 包，按 u1-l2 的方法确认装到 `opp/vendors/<vendor>_nn/`。
4. **运行验证**：`bash build.sh --run_example add_example eager cust --vendor_name=custom --soc=ascend950`，核对输出与 u1-l4 记录的基线一致（加法语义未变）。
5. **迁移评审报告**：用 4.4.4 的三列清单格式，逐项说明 add_example 对 950 每一条硬件变更的暴露度；最后回答一个问题——**如果要把本实践升级为「迁移一个带 CrossCoreSetFlag 的双核流水算子」，你的评审清单需要新增哪些检查项？**（提示：Set/Wait 配对、flagId 生命周期、循环边界对齐、异常分支。）

**验收标准**：两份构建日志（self config / def config）+ 一份运行输出 + 一份评审清单，三者齐全即视为完成。方案 A 产出的 json 属于你自己的工作目录修改，练习后请勿提交到仓库（遵守「不修改源码」的边界：本实践中复制进 `config/` 的新文件用于本地学习验证，实践结束后删除即可恢复原状）。

## 6. 本讲小结

- **三套芯片名字**：`--soc` 的 soc_version 短名（ascend950）、kernel 编译用的长名（ascend950pr_9599）、`SOC_TO_ARCH` 里的 dav 架构号（3510）；源码目录的 `arch35` 标签与架构号同源但靠算子 CMakeLists 手工声明，不做程序推导。
- **`--soc` 解析是「包含匹配 + 按长度排序 + 大小写归一」**：入口在 [build.sh:1119-1146](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1119-L1146)，透传为 cmake 的 `ASCEND_COMPUTE_UNIT`；不传时默认只编 ascend910b。
- **构建层判定有两条路**：`config/<soc>/*_binary.json` 存在走「自配置」（json 是输入，逐 dtype 槽位产出预编译二进制）；否则 grep def 里的 `.AddConfig("<soc>"` 走「def 配置」（json 是输出，自动生成）。三个 `message(STATUS)` 路标：`self config` / `def config` / `not supported`。
- **`.AddConfig()` 的判定是文本 grep 而非语义解析**：soc 名必须是字面量字符串，写成变量或宏会导致算子被判不支持。
- **arch 目录隔离由两张等长表驱动**：`SUPPORT_COMPUTE_UNIT` 与 `SUPPORT_TILING_DIR` 按下标配对（`find_value_by_key`），长度不等直接 FATAL_ERROR；非空 tiling_dir 是「叠加收集」，根目录与子目录的 tiling 都会编入。
- **跨平台迁移 = 构建层开闸门 + 源码层按硬件 diff 重写**：四步法（计算单元 dtype → 搬运单元 → 逐项修改 → 参考样例），四类硬件变更（搬运/计算/存储/其他），以及「在 A2 上能跑」不等于「同步正确」——950 要求核间同步信号量严格一一匹配。

## 7. 下一步学习建议

至此第 9 单元（扩展开发与二次贡献）与整套学习手册的正文全部结束。建议从三个方向继续：

1. **横向读一个「双代际共存」的真实算子**：gelu 是纯 950 算子，形态最简单。找一个在 910b 与 950 上都交付、且 tiling 分 `arch22`/`arch35`（或 kernel 分 `impl`/`arch35`）两套目录的算子（可在 `docs/zh/op_list.md` 里按「执行硬件」筛），用本讲的「构建层判定 → arch 目录隔离 → tiling key 编码」框架独立读通它，检验你是否真的掌握了迁移机制。
2. **把迁移与调试调优串起来**：迁移后的性能验收要用 u8-l2 的 `msprof op`（950 系真机）或 u8-l3 的 NPU Simulator（950 系仿真，注意 `SOC_TO_ARCH` 正是仿真库定位的钥匙）。建议挑 FAQ 三问中的一问，做一次真实的指标采集与归因。
3. **走向贡献**：如果你的迁移产生了通用价值（比如给某算子补上了缺失芯片的适配），按 u9-l3 的流程准备材料——CONTRIBUTING.md 的六步流水线、三张自查清单，以及 PR 评论 `compile` 触发的 CI 门禁（其中就包含多芯片编译矩阵，能替你验证 `AddConfig` 与 binary json 的一致性）。
