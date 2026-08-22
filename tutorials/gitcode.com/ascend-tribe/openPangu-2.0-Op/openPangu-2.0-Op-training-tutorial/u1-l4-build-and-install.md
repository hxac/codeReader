# 编译与安装：build.sh、CMake 与自定义算子 run 包

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂 `build.sh` 的命令行参数（`-c` 指定芯片、`-n` 指定算子白名单、`--disable-check-compatible` 跳过版本校验等），并理解它们如何被翻译成 CMake 变量。
2. 说清楚 `build.sh` 主流程中 `set_env` → `clean` → `cmake_config` → `build_package` 四步各自做什么、按什么顺序执行。
3. 理解 `CMakeLists.txt` 与 `cmake/config.cmake` 的分工：哪些构建目标（opapi / opsproto / optiling / ops_aclnn / ops_kernel）会被生成，产物安装到 run 包内的哪些目录。
4. 把编译出的 run 包安装到 CANN 的 `opp/vendors` 目录，并 `source` 对应的 `set_env.bash` 让算子生效。

本讲是第 1 单元的收官：u1-l3 搭好了容器和 CANN 环境，本讲把「源码」变成「可被框架调用的已安装算子包」。

## 2. 前置知识

- **Shell 包装层与构建系统分层**：本仓库的编译入口是一个 500 行不到的 Bash 脚本 `build.sh`，它本身不编译任何代码，只负责「解析参数 → 定位环境 → 调用 cmake」。真正的编译逻辑在 CMake 里。这种「薄壳脚本 + CMake」的分层是 CANN 生态工程的惯例。
- **CMake 三阶段**：配置（Configure，`cmake ..` 检查环境、生成 Makefile）、构建（Build，`cmake --build .` 真正编译链接）、安装（Install，把产物复制到安装前缀）。本仓库还多一个「打包」阶段：用 CPack 把安装产物压成一个自解压 run 包。
- **run 包**：一个带自解压头部的 shell 脚本（makeself 格式），执行它即可把内嵌的文件释放到指定目录。CANN 的软件包（包括我们编译出的自定义算子包）都用这种格式分发。
- **vendors 目录**：CANN 安装目录（如 `/usr/local/Ascend/ascend-toolkit/latest/opp/`）下的 `vendors/<厂商名>/` 子目录，用于存放第三方自定义算子。本仓库的厂商名是 `omni_training_custom_transformer`（在 CMakeLists.txt 中定义）。
- **bisheng（毕昇编译器）**：华为的 C/C++ 编译器，Ascend C 设备侧 kernel 必须用它编译。`build.sh` 启动时会校验它在 PATH 中是否可见。
- **soc_version / compute unit**：芯片型号代号，如 `ascend910b`（A2 类）、`ascend910_93`（A3 类）、`ascend950`。同一个算子在不同芯片上的 kernel 二进制不通用，所以编译时必须指明目标芯片。
- 建议先回顾 u1-l3：容器内每次新开 shell 都要 `source set_env.sh`，这直接决定了本讲 `build.sh` 能否找到 CANN 包。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ascendc/build.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh) | 编译总入口：解析 `-c`/`-n`/`-u` 等参数，定位 CANN 包，依次执行环境加载、清理、cmake 配置、构建打包 |
| [ascendc/CMakeLists.txt](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt) | 顶层 CMake 工程：定义 opapi/opsproto/optiling 等库目标、按白名单发现算子子目录、组织安装布局、配置 CPack 生成 run 包 |
| [ascendc/cmake/config.cmake](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake) | 配置阶段公共配置：检查 Python3、解析 CANN 路径、设定构建树路径体系、调用 prepare.sh 做预处理 |
| [ascendc/cmake/func.cmake](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/func.cmake) | CMake 函数库，本讲重点看 `op_add_subdirectory`：如何发现全部算子并按 `-n` 白名单过滤 |
| [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/CMakeLists.txt](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/CMakeLists.txt) | 单个算子目录的 CMakeLists：把子目录（op_host/op_kernel/tests）转交给 CMake，是「算子自动发现」的最小单元 |
| [ascendc/README.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md) | 官方编译/安装命令与产物说明，是本讲实践步骤的依据 |

## 4. 核心概念与源码讲解

### 4.1 build.sh 参数解析：从命令行到 CMake 变量

#### 4.1.1 概念说明

`build.sh` 对外的接口是一组短参数，对内的产物是一个字符串变量 `CUSTOM_OPTION`。它把每个命令行参数翻译成一条 `-DXXX=YYY` 追加到 `CUSTOM_OPTION` 里，最后一次性传给 `cmake`。理解这条「参数 → CMake 缓存变量」的翻译链，是看懂整个构建系统的钥匙：

- `-c` / `--compute-unit` → `-DASCEND_COMPUTE_UNIT=...`（目标芯片）
- `-n` / `--op-name` → `-DASCEND_OP_NAME=...`（算子白名单，多个算子用 `;` 分隔）
- `--tiling_key` → `-DTILING_KEY=...`（只编译指定 tilingKey 分支）
- `-u` / `--test` → `-DENABLE_TEST=TRUE` 等一组 UT 开关
- `--disable-check-compatible` → 把 `CHECK_COMPATIBLE` 置 false 再以 `-DCHECK_COMPATIBLE=...` 透传

脚本第一行的 `set -e` 意味着任何一条命令失败都会立即中止整个构建，所以报错位置就是真正的失败点。

#### 4.1.2 核心流程

```text
bash build.sh -n 'op_a;op_b' -c ascend910_93
        │
        ▼
while [[ $# -gt 0 ]]; case $1 in ... esac   # 逐个消费参数
        │  -n → ascend_op_name="op_a;op_b"
        │  -c → ascend_compute_unit="ascend910_93"
        ▼
CUSTOM_OPTION 追加 -DASCEND_OP_NAME=... -DASCEND_COMPUTE_UNIT=...
        │
        ▼
cmake .. ${CUSTOM_OPTION}                     # 配置阶段消费这些 -D
```

注意一个细节：`-n` 的值带分号，所以在 shell 里必须用引号包裹，否则分号会被 shell 解释为命令分隔符。

#### 4.1.3 源码精读

参数解析主体是一个 `while ... case` 循环，见 [ascendc/build.sh:L261-L352](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L261-L352)。每个 `case` 分支把 `$2` 存入变量并 `shift 2`（带值参数）或 `shift`（开关型参数）；任何未识别参数直接打印帮助并退出。

最有用的两个参数的翻译逻辑在 [ascendc/build.sh:L357-L363](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L357-L363)：只要 `ascend_compute_unit` / `ascend_op_name` 非空，就向 `CUSTOM_OPTION` 追加 `-DASCEND_COMPUTE_UNIT` / `-DASCEND_OP_NAME`。这一步就是 `-c`/`-n` 生效的全部秘密。

跳过版本校验的开关在 [ascendc/build.sh:L303-L306](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L303-L306)，它同时接受 `--disable-check-compatible` 和拼写变体 `--disable-check-compatiable`（兼容历史拼写错误），把 `CHECK_COMPATIBLE` 置为 `false`。

各参数的官方说明就是脚本内建的 `help_info` 函数，见 [ascendc/build.sh:L46-L65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L46-L65)，其中写明：`-c` 缺省值是 `ascend910_93`，`-n` 缺省值是「全部算子」。

UT 模式 `-u` 的翻译在 [ascendc/build.sh:L396-L405](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L396-L405)，一次追加 `-DENABLE_TEST=TRUE -DTESTS_UT_OPS_TEST=TRUE -DENABLE_UT_EXEC=TRUE` 三个变量，为第 8 单元的测试讲义埋下伏笔。

#### 4.1.4 代码实践

1. **实践目标**：不编译任何东西，仅验证帮助信息与源码一致。
2. **操作步骤**：进入 `training/ascendc` 目录，执行 `bash build.sh -h`（或 `--help`）。
3. **需要观察的现象**：终端打印 Usage 与参数表，脚本立即退出、不创建 build 目录。
4. **预期结果**：打印内容与 [ascendc/build.sh:L46-L65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L46-L65) 的 `echo` 逐行对应。该命令只读不写，任何环境都可运行。（本讲义写作环境未执行，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `-n 'ai_infra_aggregate_hidden;ai_infra_aggregate_hidden_grad'` 必须加引号？

**答案**：分号 `;` 在 shell 中是命令分隔符。不加引号时，shell 会把字符串在分号处切断，`-n` 只拿到第一个算子名，后半段被当作新命令执行（通常会报 command not found，且 `set -e` 会让脚本退出）。加引号后整个字符串作为一个参数传给 `build.sh`，再由 CMake 把 `;` 解释为列表分隔符。

**练习 2**：执行 `bash build.sh -c ascend910_93 --foo bar` 会发生什么？

**答案**：`--foo` 不匹配 `case` 的任何分支，落入 `*)` 默认分支（[ascendc/build.sh:L347-L350](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L347-L350)）：打印帮助信息并以退出码 1 结束，不会开始编译。

**练习 3**：`--tiling_key` 参数最终落到哪个 CMake 变量？依据是什么？

**答案**：`TILING_KEY`，经 [ascendc/build.sh:L373-L375](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L373-L375) 追加为 `-DTILING_KEY=${TILING_KEY}`；随后 CMakeLists.txt 的 `add_ops_tiling_keys(OP_NAME "ALL" TILING_KEYS ${TILING_KEY})`（[ascendc/CMakeLists.txt:L272-L275](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L272-L275)）消费它，只编译指定 tilingKey 的 kernel 分支。

### 4.2 build.sh 的环境定位与主流程：fallback、bisheng 校验与四步执行

#### 4.2.1 概念说明

参数解析之后、编译之前，`build.sh` 要回答两个问题：「CANN 包在哪」和「编译器在哪」。

- **CANN 包定位（五级 fallback）**：`ASCEND_CANN_PACKAGE_PATH` 依次尝试 `-p` 显式参数 → `ASCEND_HOME_PATH` 环境变量 → `ASCEND_OPP_PATH` 推导 → 非 root 用户默认目录 `~/Ascend/ascend-toolkit/latest` → root 用户默认目录 `/usr/local/Ascend/...`。这解释了 u1-l3 中「每次新开 shell 必须 source set_env.sh」的深层原因：source 之后 `ASCEND_HOME_PATH` 被导出，第二级 fallback 才能命中。
- **bisheng 校验**：`set_env` 函数 source CANN 包内的 `setenv.bash`，然后检查 `which bisheng`。找不到就报错退出——设备侧 kernel 离开毕昇编译器无法构建。

#### 4.2.2 核心流程

主流程（[ascendc/build.sh:L438-L497](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L438-L497)）按固定顺序执行：

```text
set_env            # source setenv.bash + 校验 bisheng
   ↓
clean              # rm -rf build/，mkdir build/ output/
   ↓
ccache 探测        # 系统有 ccache 就生成 bisheng 包装脚本加速重编
   ↓
cd build/
   ├─ -u 模式     → cmake_config + build_ut（transformer_op_host_ut / transformer_op_api_ut）
   ├─ --ophost/--opapi → build_lib（只编库）
   ├─ -b host     → cmake_config -DENABLE_OPS_KERNEL=OFF + build_host（只打包 host 侧）
   ├─ -b kernel   → cmake_config -DENABLE_OPS_HOST=OFF + build_kernel
   └─ 默认        → cmake_config + build_package（完整编译 + 打 run 包）
```

`build_package()` 的函数体只有一行 `build package`（[ascendc/build.sh:L166-L176](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L166-L176)），即 `cmake --build . --target package`——构建 `package` 目标，由 CPack 接管（见 4.5 节）。

并行度也在这一段决定：[ascendc/build.sh:L422-L430](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L422-L430) 取 CPU 逻辑核数 × 2 作为 `-j` 参数，可用环境变量 `OPS_CPU_NUMBER` 覆盖。

#### 4.2.3 源码精读

五级 fallback 的完整实现见 [ascendc/build.sh:L407-L420](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L407-L420)。五级都落空时打印错误并 `exit 1`，错误提示会建议用 `-p|--package-path` 显式指定——这是无 NPU 环境下最常见的第一个报错。

`set_env` 见 [ascendc/build.sh:L72-L82](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L72-L82)：先 `source $ASCEND_CANN_PACKAGE_PATH/bin/setenv.bash`，再校验 `BISHENG_REAL_PATH` 非空。注意 `|| echo "0"` 只兜住 source 的返回值，bisheng 缺失仍是硬错误。

`clean` 见 [ascendc/build.sh:L84-L91](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L84-L91)：删除并重建 `build/`，同时 `mkdir -p` 确保 `output/` 存在。**每次构建都是全量清理**，增量编译只能靠 ccache（[ascendc/build.sh:L442-L456](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L442-L456) 自动探测系统 ccache，并用 `gen_bisheng`（[ascendc/build.sh:L141-L164](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L141-L164)）生成一个「ccache + bisheng」的包装脚本塞进 PATH）。

ccache 与 CANN 路径最终都汇入 [ascendc/build.sh:L432](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L432)：`CUSTOM_OPTION` 追加 `-DCUSTOM_ASCEND_CANN_PACKAGE_PATH=... -DCHECK_COMPATIBLE=...`，配置阶段由此拿到完整上下文。

`-b host` 分支见 [ascendc/build.sh:L481-L487](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L481-L487)：关闭 `ENABLE_OPS_KERNEL` 只编 host 侧，打完包后把 `build/` 下的 `.run` 拷贝到 `output/`——这是源码中「run 包如何到达 output 目录」的唯一显式路径。

#### 4.2.4 代码实践

1. **实践目标**：体验 CANN 包定位的 fallback 顺序，理解环境变量的作用。
2. **操作步骤**：在一台装了 CANN 的容器里依次执行并对比：
   - `env -i bash build.sh -n ai_infra_aggregate_hidden -c ascend910_93`（干净环境）
   - `source /usr/local/Ascend/ascend-toolkit/set_env.sh && bash build.sh -n ai_infra_aggregate_hidden -c ascend910_93`
   - `bash build.sh -n ai_infra_aggregate_hidden -c ascend910_93 -p /usr/local/Ascend/ascend-toolkit/latest`
3. **需要观察的现象**：第一种大概率在「Please set the toolkit package installation directory」处退出；第二、三种能走到 cmake 配置。
4. **预期结果**：三条命令的成败差异正好对应 [ascendc/build.sh:L407-L420](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L407-L420) 的 fallback 链。待本地验证（本讲义写作环境无 CANN，未执行）。

#### 4.2.5 小练习与答案

**练习 1**：为什么每次重新执行 `build.sh` 都是全量重编？如何缓解？

**答案**：`clean()` 每次删除整个 `build/` 目录（[ascendc/build.sh:L84-L91](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L84-L91)）。缓解手段是安装 ccache：脚本探测到后会经 `gen_bisheng` 生成包装脚本（[ascendc/build.sh:L442-L456](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L442-L456)），让 bisheng 的编译结果跨构建缓存命中；也可用 `--ccache <路径>` 显式指定。

**练习 2**：`-b host` 与 `-b kernel` 分别向 cmake 传了什么开关？

**答案**：`-b host` 传 `-DENABLE_OPS_KERNEL=OFF`（不编设备侧 kernel，见 [ascendc/build.sh:L481-L483](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L481-L483)）；`-b kernel` 传 `-DENABLE_OPS_HOST=OFF`（不编 host 侧 tiling/proto，见 [ascendc/build.sh:L488-L490](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L488-L490)）。两者用于只改了一侧代码时的快速验证。

**练习 3**：`set_env` 中 `source ... || echo "0"` 为什么不能省略 `|| echo "0"`？

**答案**：脚本开了 `set -e`（[ascendc/build.sh:L10](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L10)）。`setenv.bash` 在部分环境下可能返回非零（例如提示性输出），没有兜底就会让整个构建在 source 一步直接中止，用户看不到后面更有价值的 bisheng 校验报错。

### 4.3 CMakeLists.txt：算子自动发现、白名单过滤与五大构建目标

#### 4.3.1 概念说明

顶层 `CMakeLists.txt` 回答三个问题：

1. **编哪些算子**——不维护手工清单，而是用 glob 扫描 `src/ops-transformer` 下所有带 `CMakeLists.txt` 的目录自动发现算子，再用 `ASCEND_OP_NAME` 白名单过滤。新增算子只要建好目录和 CMakeLists，无需修改顶层文件。
2. **编成什么**——五个关键目标：

| 目标 | 类型 | 产物 | 安装位置（run 包内） | 对应四层结构 |
| --- | --- | --- | --- | --- |
| `opapi` | SHARED 库 | `cust_opapi.so` | `packages/vendors/<厂商>/op_api/lib` | op_api（aclnn 接口实现） |
| `opsproto` | SHARED 库 | `cust_opsproto_rt2.0.so` | `.../op_proto/lib/linux/<arch>` | op_def（算子原型注册） |
| `optiling` | SHARED 库 | `cust_opmaster_rt2.0.so` | `.../op_tiling/lib/linux/<arch>` | op_host（Tiling 实现） |
| `ops_aclnn` | STATIC 库 | 中间产物 | 打入 opapi | aclnn 生成代码 |
| `ops_kernel` | 自定义目标 | 每个 compute_unit 的 kernel 二进制 | `.../op_impl/ai_core/tbe/kernel/...` | op_kernel（设备侧） |

3. **装到哪里**——所有 install 路径都以 `packages/vendors/${VENDOR_NAME}/` 为前缀，`VENDOR_NAME` 固定为 `omni_training_custom_transformer`。这个前缀就是安装后 `opp/vendors/` 下厂商目录的雏形。

#### 4.3.2 核心流程

```text
CMakeLists.txt 配置期
   ├─ include config/func/intf/variables/ut 五个模块
   ├─ 定义 opapi / opsproto / optiling 库目标与 install 规则
   ├─ add_subdirectory(common) + add_subdirectory(utils)
   ├─ op_add_subdirectory()            # glob 发现算子 + 白名单过滤
   ├─ 非 ascend950 → 从列表剔除 attention_pioneer 两算子
   ├─ foreach(OP_DIR) add_subdirectory # 各算子的 CMakeLists 把 _def.cpp 挂到目标上
   ├─ 收集全部 _def.cpp → 推导要生成的 aclnn_*.cpp / *_proto.cpp 文件名
   ├─ 注册 opbuild_gen_* 规则（调用 CANN 的 op_build 工具生成上述代码）
   └─ ops_kernel：为每个 compute_unit 注册二进制编译目标
```

关键认知：**aclnn 接口源码是编译期生成的**。算子目录里只有 `_def.cpp`（原型注册），配置期收集它们后，用 CANN 包内的 `op_build` 工具自动生成 `aclnn_<算子名>.cpp` 等源码再编译。这也解释了 u1-l2 的结论「多数算子目录看不到 op_api 源码」——生成物落在构建树的 `autogen/` 目录。

#### 4.3.3 源码精读

工程骨架与三个关键缓存变量见 [ascendc/CMakeLists.txt:L9-L24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L9-L24)：`ASCEND_COMPUTE_UNIT` 缺省 `ascend910_93`、`ASCEND_OP_NAME` 缺省 `ALL`、`VENDOR_NAME` 为 `omni_training_custom_transformer`——`build.sh -c/-n` 传入的正是前两个。

算子发现与芯片裁剪见 [ascendc/CMakeLists.txt:L297-L318](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L297-L318)：先 `add_subdirectory` 公共的 common 与 utils，再调用 `op_add_subdirectory` 得到算子目录列表；当 `ASCEND_COMPUTE_UNIT` 不是 `ascend950` 时，用 `list(FILTER ... EXCLUDE)` 把 `ai_infra_attention_pioneer` 与 `ai_infra_attention_pioneer_backward` 从列表剔除（A3 芯片不编这两个算子）；最后逐目录 `add_subdirectory`。

白名单过滤的实现在 [ascendc/cmake/func.cmake:L41-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/func.cmake#L41-L80)：`file(GLOB)` 匹配 `src/ops-transformer/**/**/CMakeLists.txt` 及 `ophost/CMakeLists.txt` 两种形态，目录名即算子名；[ascendc/cmake/func.cmake:L62-L68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/func.cmake#L62-L68) 判断「`ASCEND_OP_NAME` 非 ALL 且当前算子不在列表中」就 `continue()` 跳过——这就是 `-n` 白名单唯一的生效点。

单算子目录的 CMakeLists 非常薄，见 [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/CMakeLists.txt:L10-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/CMakeLists.txt#L10-L17)：遍历自己的子目录，凡是有 `CMakeLists.txt` 的就 `add_subdirectory`；非测试构建时剔除 `tests`。真正的挂接（把 `_def.cpp` 加进 opsproto、把 `_tiling.cpp` 加进 optiling）发生在 `op_host/CMakeLists.txt` 里。

三个 host 侧库目标的定义见 [ascendc/CMakeLists.txt:L81-L104](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L81-L104)（`op_host_aclnn`/`Inner`/`Exc` 三个 EXCLUDE_FROM_ALL 共享库，作为 aclnn 生成器的输入载体）、[ascendc/CMakeLists.txt:L106-L154](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L106-L154)（`opapi` 库，输出名 `cust_opapi`，安装到 `op_api/lib`）、[ascendc/CMakeLists.txt:L157-L199](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L157-L199)（`opsproto`，输出名 `cust_opsproto_rt2.0`，安装到 `op_proto/lib`）、[ascendc/CMakeLists.txt:L202-L253](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L202-L253)（`optiling`，输出名 `cust_opmaster_rt2.0`，安装到 `op_tiling/lib`）。

aclnn 生成代码的文件名推导见 [ascendc/CMakeLists.txt:L352-L364](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L352-L364)：对每个 `_def.cpp` 源文件去掉 `_def` 后缀得到算子名，进而推出 `autogen/aclnn_<算子名>.cpp/.h` 与 `<算子名>_proto.cpp/.h`。真正调用 CANN `op_build` 工具的生成规则见 [ascendc/CMakeLists.txt:L560-L608](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L560-L608)（三个 `opbuild_gen_*` 自定义目标，命令行里设 `OPS_ACLNN_GEN=1` 等环境变量后执行 `${OP_BUILD_TOOL}`）。

设备侧 kernel 的编译入口见 [ascendc/CMakeLists.txt:L687-L700](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L687-L700)：`foreach(compute_unit ${ASCEND_COMPUTE_UNIT})` 为每款芯片注册一个二进制编译目标；kernel 源码本身则被安装到 impl 目录供二次构建使用（[ascendc/CMakeLists.txt:L631-L664](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L631-L664)，glob 每个算子的 `op_kernel/*.cpp|*.h` 安装到 `.../op_impl/ai_core/tbe/<厂商>_impl/ascendc/<算子名>`）。

#### 4.3.4 代码实践

1. **实践目标**：不看安装过程，仅凭 CMake 源码推导出 run 包的目录结构。
2. **操作步骤**：通读 4.3.3 列出的各 install 语句，把每个目标的 `DESTINATION` 拼到 `packages/vendors/omni_training_custom_transformer/` 前缀下，画成一棵目录树。
3. **需要观察的现象**：你画出的树应当包含 `op_api/lib`、`op_proto/lib/linux/<arch>`、`op_impl/ai_core/tbe/op_tiling/lib/linux/<arch>`、`op_impl/ai_core/tbe/kernel/<...>`、根部的 `install.sh`/`version.info` 等节点。
4. **预期结果**：与 4.5 节安装后 `ls` 出来的真实 vendors 目录一致（本实践为纯源码阅读型，随时可做；对照真实环境待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：新增一个算子目录后，需要修改顶层 `CMakeLists.txt` 把它加进编译清单吗？

**答案**：不需要。算子是 glob 自动发现的（[ascendc/cmake/func.cmake:L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/func.cmake#L45) 扫描 `**/**/CMakeLists.txt`）。只要新算子目录（或其 `op_host` 子目录）里有 `CMakeLists.txt`，就会被自动纳入；除非它只支持 ascend950，才受 [ascendc/CMakeLists.txt:L304-L307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L304-L307) 的剔除规则影响。

**练习 2**：`opsproto` 与 `optiling` 两个库分别对应四层结构中的哪一层？加载时机有何不同？

**答案**：`opsproto`（`cust_opsproto_rt2.0.so`）承载 `_def.cpp` 原型注册，对应 op_def 层，图构图/算子信息查询时加载；`optiling`（`cust_opmaster_rt2.0.so`）承载 `_tiling.cpp`，对应 op_host 层，算子执行前的切分阶段调用。两者都在 host 侧运行，与设备侧的 kernel 二进制（ops_kernel 目标产物）相对应。

**练习 3**：为什么 `attention_pioneer` 在 `-c ascend910_93` 时不会被编译？用源码说明。

**答案**：[ascendc/CMakeLists.txt:L304-L307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L304-L307) 中，只要 `ASCEND_COMPUTE_UNIT` 不等于 `ascend950`，就用 `list(FILTER OP_DIR_LIST EXCLUDE REGEX "ai_infra_attention_pioneer...")` 把这两个目录从参与 `add_subdirectory` 和 kernel 编译的列表中剔除。该算子是 arch35（A3 的 950 形态）专属实现，这也与 u1-l2「芯片适配分编译期与运行期两层」的结论呼应。

### 4.4 cmake/config.cmake：配置阶段的环境检查与预处理

#### 4.4.1 概念说明

`config.cmake` 在 cmake 配置一开始就被 include（[ascendc/CMakeLists.txt:L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L20)），干三件事：

1. **环境检查**：必须有 Python3（生成适配代码要用）；解析并打印 CANN 包路径。
2. **建立路径体系**：定义构建树内的 `autogen/`（生成代码）、`binary/`（kernel 二进制）、`impl/`（适配脚本）目录，以及 run 包内的安装前缀；把安装前缀 `CMAKE_INSTALL_PREFIX` 修正为 `build/../output`。
3. **调用 prepare.sh 预处理**：把算子白名单、芯片型号、tilingKey 等透传给 CANN 的预处理脚本，为 kernel 编译准备工程文件。

此外两个重要开关也在这里：`ENABLE_OPS_KERNEL` 默认 ON，但 `-u` 测试模式下强制 OFF（UT 不编设备侧）；`CHECK_COMPATIBLE` 打开时执行 CANN 版本配套校验并取回版本号用于 run 包命名。

#### 4.4.2 核心流程

```text
config.cmake（配置期执行）
   ├─ find_package(Python3) → 失败则 FATAL_ERROR
   ├─ 解析 ASCEND_CANN_PACKAGE_PATH（-D 传入 > ASCEND_HOME_PATH > ASCEND_OPP_PATH/.. > /usr/local/Ascend/latest）
   ├─ 定义构建树路径 + OP_BUILD_TOOL = <CANN>/tools/opbuild/op_build
   ├─ 定位 ASCEND_PROJECT_DIR（CANN 包内的工程模板，含 install.sh 等脚本）
   ├─ CHECK_COMPATIBLE=ON → 运行 check_version_compatible.py，取回 CANN_VERSION
   └─ ENABLE_OPS_KERNEL 且非 PREPARE_BUILD → 运行 prepare.sh
        参数：-s 源码树 -b 构建树 --ascend-compute_unit ... --ascend-op_name ...
```

#### 4.4.3 源码精读

Python3 检查见 [ascendc/cmake/config.cmake:L14-L18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L14-L18)，找不到直接 `FATAL_ERROR` 终止配置。

CANN 路径解析见 [ascendc/cmake/config.cmake:L21-L30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L21-L30)：优先级为 `CUSTOM_ASCEND_CANN_PACKAGE_PATH`（即 `build.sh` 传的 `-DCUSTOM_ASCEND_CANN_PACKAGE_PATH`）＞ 环境变量 `ASCEND_HOME_PATH` ＞ `ASCEND_OPP_PATH` 的上级 ＞ 兜底 `/usr/local/Ascend/latest`，随后 `message(STATUS ...)` 把结果打印进 configure 日志——这是排查环境问题第一个该看的日志行。

开关定义见 [ascendc/cmake/config.cmake:L37-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L37-L43)：`ENABLE_OPS_HOST` 默认 ON；`ENABLE_OPS_KERNEL` 默认 ON、但 `ENABLE_TEST`（`-u`）时改为 OFF——所以 UT 构建不需要真实编译 kernel。

构建树路径与 `OP_BUILD_TOOL` 见 [ascendc/cmake/config.cmake:L53-L59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L53-L59)：`autogen`、`binary`、`impl` 三个输出目录都挂在 `${CMAKE_CURRENT_BINARY_DIR}`（即 `ascendc/build/`）下；`OP_BUILD_TOOL` 指向 CANN 包的 `tools/opbuild/op_build`，就是 4.3 节 aclnn 代码生成所调用的工具。

安装前缀体系见 [ascendc/cmake/config.cmake:L67-L76](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L67-L76)：`ASCEND_PROJECT_DIR` 取 CANN 包内 `tools/ascend_project`（或旧版 `tools/op_project_templates/ascendc/customize`）模板；`IMPL_INSTALL_DIR` 等安装路径全部以 `packages/vendors/${VENDOR_NAME}/...` 开头。安装前缀修正见 [ascendc/cmake/config.cmake:L151-L154](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L151-L154)：默认值被强制改写为 `${CMAKE_CURRENT_BINARY_DIR}/../output`，把 install 阶段产物引向 `output/` 目录。

版本配套校验见 [ascendc/cmake/config.cmake:L174-L193](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L174-L193)：`CHECK_COMPATIBLE` 打开时执行 [ascendc/cmake/scripts/check_version_compatible.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/scripts/check_version_compatible.py) 的 `check_code_compatible` 子命令，校验源码与 CANN 包版本是否配套，成功则把返回的版本号存入 `CANN_VERSION`（供 run 包命名使用），失败则 `FATAL_ERROR`。README FAQ 中「版本校验失败时加 `--disable-check-compatible`」即对应此机制；由于 `build.sh` 中该开关默认即为 `false`（[ascendc/build.sh:L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L22)），该参数主要用于 CI 或直接 cmake 场景下显式关闭。

预处理见 [ascendc/cmake/config.cmake:L195-L242](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L195-L242)：分号列表被转成 `::` 分隔后传给 [ascendc/cmake/scripts/prepare.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/scripts/prepare.sh)，参数包括 `--ascend-compute_unit`、`--ascend-op_name`、`--tiling-key`、`--check-compatible` 等——`build.sh` 的所有选择在此完成「最后一公里」传递。

configure 日志会打印关键变量，见 [ascendc/cmake/config.cmake:L160-L167](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L160-L167)：`ASCEND_COMPUTE_UNIT`、`ASCEND_OP_NAME`、`TILING_KEY` 逐一 `message(STATUS)`，用来确认 `-c`/`-n` 是否真的传进来了。

#### 4.4.4 代码实践

1. **实践目标**：追踪一条参数传递链，验证「命令行 → CMake 变量 → prepare.sh 参数」三段接力。
2. **操作步骤**：以 `-c ascend910_93` 为例，在三个文件中各找到一处代码：`build.sh` 中 case 分支与 `-D` 追加（L271-272、L357-359）；`config.cmake` 中的打印（L164）与传参（L225）；`prepare.sh` 中接收该参数的位置（用 Grep 搜 `ascend-compute_unit`）。
3. **需要观察的现象**：三段代码用同一个字符串 `ascend-compute_unit` / `ASCEND_COMPUTE_UNIT` 串联。
4. **预期结果**：画出传递链 `CLI -c → build.sh 变量 → -DASCEND_COMPUTE_UNIT → config.cmake 打印 → prepare.sh --ascend-compute_unit`。纯源码阅读型实践，无环境依赖。

#### 4.4.5 小练习与答案

**练习 1**：`-u`（UT 模式）下为什么不需要编译设备侧 kernel？

**答案**：[ascendc/cmake/config.cmake:L39-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L39-L43) 中 `ENABLE_TEST` 为真时把 `ENABLE_OPS_KERNEL` 置为 OFF，而顶层 [ascendc/CMakeLists.txt:L687-L690](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L687-L690) 只在该开关为真时才注册 kernel 编译目标。UT 用 faker/stub 在 host 侧模拟上下文（第 8 单元详讲），不依赖真实 kernel 二进制。

**练习 2**：configure 日志里看到 `ASCEND_COMPUTE_UNIT=ascend910_93` 说明什么？看不到又说明什么？

**答案**：说明 `-c` 参数成功通过 `build.sh`（L357-359）进入 CMake 缓存变量并被 [ascendc/cmake/config.cmake:L164](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L164) 打印，白名单与芯片裁剪都基于它生效；若打印的是缺省值或空值，说明参数拼写或引号有问题，应先检查命令行而不是怀疑编译器。

**练习 3**：run 包文件名中的版本号 `<cann_version>` 从哪来？

**答案**：来自 `CHECK_COMPATIBLE` 校验的返回值 `CANN_VERSION`（[ascendc/cmake/config.cmake:L182-L192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L182-L192)），随后被 CPack 的 `CPACK_PACKAGE_FILE_NAME` 引用（见 4.5 节）。未开启校验时该变量为空，包名中会出现连续两个连字符，README 中「CANN-omni_training_custom_ops--linux.\<arch\>.run」的双横线正是这个原因。

### 4.5 CPack 打包与 run 包安装

#### 4.5.1 概念说明

`build_package` 构建的 `package` 目标由 CPack 接管：先把所有 `install()` 规则的产物收集到 staging 目录，再用 makeself 脚本把它们封装成一个可执行的自解压 run 包。安装这个 run 包，本质是把 `packages/vendors/omni_training_custom_transformer/` 整棵目录树释放到 CANN 的 `opp/vendors/` 下，随后 source 厂商目录里的 `set_env.bash` 让运行时能找到它。

安装三要素（来自 README）：

1. `chmod +x` 赋予执行权限；
2. `./xxx.run --quiet --install-path=<CANN>/ascend-toolkit/latest/opp` 指定释放目录；
3. `source <该目录>/opp/vendors/omni_training_custom_transformer/bin/set_env.bash` 生效。

#### 4.5.2 核心流程

```text
cmake --build . --target package
   ↓ CPack（External 生成器）
staging：收集 op_api / op_proto / op_tiling / kernel 二进制 / impl 脚本 / install.sh / version.info
   ↓ makeself（CANN 包内 makeself.cmake）
输出 CANN-omni_training_custom_ops-<cann_version>-linux.<arch>.run
   ↓ 手动安装（--install-path）
/usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_training_custom_transformer/...
   ↓ source .../bin/set_env.bash
运行时（torch_npu / aclnn 调用）可发现自定义算子
```

#### 4.5.3 源码精读

安装脚本注入见 [ascendc/CMakeLists.txt:L702-L717](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L702-L717)：把 CANN 模板里的 `install.sh`/`upgrade.sh` 拷到构建树，并用 `sed` 把其中的 `vendor_name=customize` 替换成 `omni_training_custom_transformer` 后随包分发——run 包安装时执行的 install.sh 就来自这里。

版本文件见 [ascendc/CMakeLists.txt:L719-L732](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L719-L732)：调用 `gen_version_info.sh` 生成 `version.info` 并装到厂商目录根部，供后续版本配套校验使用。

CPack 配置见 [ascendc/CMakeLists.txt:L739-L750](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L739-L750)：包名模板为 `CANN-omni_training_custom_ops-${CANN_VERSION}-linux.${CMAKE_SYSTEM_PROCESSOR}.run`；生成器是 `External`，打包脚本指向 CANN 包内 `${ASCEND_CMAKE_DIR}/makeself.cmake`（makeself 自解压封装），包输出目录为 `${CMAKE_BINARY_DIR}`（即 `ascendc/build/`）。

官方安装命令见 [ascendc/README.md:L253-L264](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L253-L264)：`chmod +x` → `--quiet --install-path=/usr/local/Ascend/ascend-toolkit/latest/opp` → `source /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_training_custom_transformer/bin/set_env.bash`；README 并注明安装后的落点是 `.../opp/vendors/`。编译成功标志与产物路径说明见 [ascendc/README.md:L244-L251](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L244-L251)：出现 `Self-extractable archive "...run" successfully created.` 即打包成功，产物是 `CANN-omni_training_custom_ops-<cann_version>-linux.<arch>.run`。

关于 run 包的具体落盘位置要注意：CPack 的包目录是构建树（`CPACK_PACKAGE_DIRECTORY=${CMAKE_BINARY_DIR}`，[ascendc/CMakeLists.txt:L743](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L743)），`build.sh` 仅在 `-b host` 分支显式把 `.run` 拷到 `output/`（[ascendc/build.sh:L485-L487](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L485-L487)）。README 记载产物在 `output/` 目录；若在那里没找到，应先去 `ascendc/build/` 下查找。

#### 4.5.4 代码实践

1. **实践目标**：跑通「安装 + source + 验证目录」三步，确认算子包真正就位。
2. **操作步骤**（在有 CANN 环境的容器内，紧接第 5 节综合实践产出的 run 包）：
   ```bash
   cd /home/code/omni-ops/training/ascendc/output      # 若无 .run，去 ../build/ 找
   chmod +x CANN-omni_training_custom_ops-*-linux.*.run
   ./CANN-omni_training_custom_ops-*-linux.*.run --quiet --install-path=/usr/local/Ascend/ascend-toolkit/latest/opp
   source /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_training_custom_transformer/bin/set_env.bash
   ls /usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_training_custom_transformer/
   ```
3. **需要观察的现象**：最后一条 `ls` 应列出 `op_api`、`op_proto`、`op_impl` 等目录（对照 4.3.4 你画的目录树）；source 无报错。
4. **预期结果**：目录结构与 4.3.3 的 install 规则一一对应。待本地验证（本讲义写作环境无 CANN/NPU，未执行）。

#### 4.5.5 小练习与答案

**练习 1**：安装 run 包时 `--install-path` 指向 `.../ascend-toolkit/latest/opp`，为什么不指向 `vendors` 本身？

**答案**：run 包内部的目录树已经带 `packages/vendors/<厂商名>/...` 前缀（见 4.3 节各 install 规则），install.sh 会把它释放并整理到 `--install-path` 下的 `vendors/` 中。指向 `opp` 是让释放结果恰好落在 `opp/vendors/omni_training_custom_transformer/`，多拼一层 `vendors` 会造成 `vendors/vendors` 的双层错误。

**练习 2**：安装后为什么还要再 source 一次厂商目录下的 `set_env.bash`？这与 u1-l3 的 set_env.sh 是什么关系？

**答案**：u1-l3 的 `set_env.sh` 只初始化 CANN 主包环境；自定义算子装进 `opp/vendors/` 后，运行时靠厂商目录的 `set_env.bash`（安装产物的一部分，来自 [ascendc/CMakeLists.txt:L702-L717](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L702-L717) 注入了厂商名的脚本）把 vendors 路径追加进 `ASCEND_OPP_PATH`/`LD_LIBRARY_PATH` 等搜索路径。两次 source 作用于不同层级，缺一不可。

**练习 3**：run 包里的 `install.sh` 与仓库里的 `build.sh` 是什么关系？

**答案**：完全不同的两个脚本。`build.sh` 是编译入口（开发期）；`install.sh` 是 CANN 工程模板自带、随 run 包分发的安装器（部署期），配置期由 [ascendc/CMakeLists.txt:L708-L713](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L708-L713) 从 `${ASCEND_PROJECT_DIR}/scripts/` 拷贝并把 `vendor_name=customize` 替换为本仓库厂商名。

## 5. 综合实践

把本讲全部知识串成一次真实的「编译 → 安装 → 验证」。以下命令均来自仓库 README 与 build.sh 源码，在装好 CANN（A3 镜像）的容器内执行。

**第一步：编译指定算子。**

```bash
cd /home/code/omni-ops/training/ascendc
bash build.sh -n 'ai_infra_aggregate_hidden;ai_infra_aggregate_hidden_grad' -c ascend910_93
```

执行时对照本讲内容观察四个checkpoint：

1. 参数被接受后，configure 日志应出现 `ASCEND_COMPUTE_UNIT=ascend910_93` 与 `ASCEND_OP_NAME=ai_infra_aggregate_hidden;ai_infra_aggregate_hidden_grad`（由 [ascendc/cmake/config.cmake:L160-L167](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/config.cmake#L160-L167) 打印）。
2. `build/` 目录被重建（clean 的效果），其中出现 `autogen/`（aclnn 生成代码）与 `binary/`（kernel 二进制）。
3. 构建末尾出现 README 描述的成功标志：`Self-extractable archive "CANN-omni_training_custom_ops-...run" successfully created.`
4. **记录 run 包的完整路径**：优先在 `output/` 找；没有则到 `build/` 找（依据见 4.5.3）。

**第二步：安装并生效。** 执行 4.5.4 的三步（chmod + 安装到 `.../latest/opp` + source 厂商 set_env.bash）。

**第三步：验证。** `ls` 厂商目录，确认 `op_api`、`op_proto`、`op_impl` 三类子目录存在，并 `ls op_impl/ai_core/tbe/kernel/` 观察按芯片组织的 kernel 二进制目录名是否含 `ascend910_93`。

**无 NPU/CANN 环境的替代方案**（源码阅读型）：手动执行到 configure 阶段即可收获大部分知识——

```bash
cd training/ascendc && mkdir -p build && cd build
cmake .. -DBUILD_OPEN_PROJECT=ON -DCMAKE_CXX_FLAGS="-w" -DCMAKE_C_FLAGS="-w" \
  -DASCEND_COMPUTE_UNIT=ascend910_93 \
  -DASCEND_OP_NAME="ai_infra_aggregate_hidden;ai_infra_aggregate_hidden_grad" \
  -DCUSTOM_ASCEND_CANN_PACKAGE_PATH=/usr/local/Ascend/latest
```

这组 `-D` 参数正是 `build.sh` 的 `cmake_config` 在默认路径下会拼出的内容（[ascendc/build.sh:L39-L40](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L39-L40)、[L357-L363](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L357-L363)、[L432](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L432)）。即使因缺少 CANN 包在 Python3 检查或路径解析处失败，把报错行号对照 config.cmake 源码，也能完整验证「参数 → 变量 → 日志」这条链路。本讲义写作环境即属此类，以上命令均未实际执行，**待本地验证**。

## 6. 本讲小结

- `build.sh` 是薄壳：把 `-c`/`-n`/`--tiling_key`/`-u` 等参数翻译成 `-D` CMake 变量（[L261-L352](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L261-L352)、[L357-L363](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L357-L363)），主流程固定为 `set_env → clean → cmake_config → build_package`。
- CANN 包路径靠五级 fallback 定位（[build.sh:L407-L420](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/build.sh#L407-L420)），bisheng 编译器缺失是硬错误；每次构建全量清理，增量靠 ccache。
- 算子目录由 glob 自动发现、按 `-n` 白名单过滤（[func.cmake:L41-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/cmake/func.cmake#L41-L80)）；非 ascend950 芯片会剔除 attention_pioneer 前反向两算子（[CMakeLists.txt:L304-L307](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L304-L307)）。
- aclnn 接口源码是配置期由 CANN 的 `op_build` 工具从 `_def.cpp` 生成的（[CMakeLists.txt:L560-L608](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L560-L608)），落在构建树 `autogen/`；run 包内的所有产物都安装在 `packages/vendors/omni_training_custom_transformer/` 前缀下。
- CPack + makeself 把安装产物封装成 `CANN-omni_training_custom_ops-<cann_version>-linux.<arch>.run`（[CMakeLists.txt:L739-L750](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/CMakeLists.txt#L739-L750)）；安装即释放到 CANN 的 `opp/vendors/`，再 source 厂商目录的 `set_env.bash` 生效。

## 7. 下一步学习建议

第 1 单元到此完结：你已经知道项目是什么（u1-l1）、算子分几层（u1-l2）、环境怎么搭（u1-l3）、怎么编译安装（本讲）。第 2 单元将进入第一个算子的逐层精读，建议按此顺序预习：

1. 先读 [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md) 与其 docs 目录，带着「这个算子算什么、输入输出长什么样」的问题进入 u2-l1。
2. 回头再看本讲的 `op_host/CMakeLists.txt`（`ai_infra_aggregate_hidden` 目录下）如何把 `_def.cpp`/`_tiling.cpp` 挂到 `opsproto`/`optiling` 目标上——这是连接「构建系统」与「算子四层结构」的桥梁。
3. 有环境的话，用本讲安装好的算子包跑一次 `tests/st` 下的精度测试（`pytest` 入口见 [ascendc/src/tests/st/pytest.ini](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/tests/st/pytest.ini)），提前感受第 8 单元的测试体系。
