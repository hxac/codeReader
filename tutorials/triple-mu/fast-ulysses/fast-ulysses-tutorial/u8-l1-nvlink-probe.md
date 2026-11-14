# u8-l1 NVLink 拓扑探测：dlopen NVML 与失败开放

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `nvlink_matrix` 的三步流程——BDF 匹配设备、扫描直连 GPU-GPU 链路、用 NVSwitch 全互连补全矩阵——以及每一步在源码里的位置。
2. 区分 NVML 的 `NOT_SUPPORTED` / `INVALID_ARGUMENT`（这是**设备答完了**：「我没有更多链路了」）与真正无法询问（NVML 缺失、太老、单个设备探不出来）——后者对应返回值 `nullopt`，两者绝不能混淆。
3. 解释为什么 `answered` 标志必须**每个设备各记一份**而不是整个扫描共享一份：共享会让「没人问成功」被误读成「链路缺失」，产生一次错误的拒绝。
4. 描述 fail-open 语义：「说不出」不等于「拒绝」，`check_nvlink` 对 `nullopt` 返回空串放行，只在矩阵**确实回答了且含有 false 对**时才拒绝；并能判读 `fast-ulysses doctor` 打印的矩阵。

本讲属于高级层，但代码本身很短（`src/nvlink.cc` 全文约 220 行），难的不是代码量，而是它对「答案的三种状态」——是、否、说不出——的严格区分。

## 2. 前置知识

- **NVML（NVIDIA Management Library）**：NVIDIA 的设备管理库（`libnvidia-ml.so.1`），`nvidia-smi` 背后就是它。本讲用到五个入口点：初始化、按 PCI 总线号取设备句柄、查某条 NVLink 是否激活、查链路对端类型（GPU 还是交换芯片）、查对端的 PCI 信息。
- **BDF（Bus/Device/Function，总线-设备-功能）**：PCI 设备的「门牌号」，形如 `domain:bus:device.function`。CUDA 用从 0 开始的序号数设备，NVML 用 PCI 总线号字符串找设备，链路的远端也用 PCI 信息标识——BDF 是两个世界之间唯一的桥。
- **NVLink 的两种互连拓扑**：一种是 GPU 直连 GPU（常见于两卡 SXM 对插）；另一种是所有 GPU 各拉若干链路进 **NVSwitch** 交换芯片，由交换芯片全互连——8 卡 DGX/HGX 机器都是这种。对传输层来说两者等价：都能直写对端内存。
- **`dlopen` / `dlsym`**：在运行期手动加载共享库、查找符号的 POSIX 接口。与编译期链接相对：编译期链接会让本库的 `.so` 声明「我依赖 `libnvidia-ml.so.1`」，没装 NVML 的机器连 `import fast_ulysses` 都会失败；`dlopen` 则把它变成运行期的一次询问。
- **fail-open（失败开放）**：一项检查在「自己无法执行」时选择**放行**而不是拒绝。与之相对的是 fail-closed（无法检查就拒绝）。哪个对取决于两种错误的代价，本讲会看到本项目如何权衡。
- 承接 u2-l1：`UlyssesGroup(require_nvlink=True)` 构造时 Python 侧先 `all_gather` 收集全组设备号，再调 C++ 的 `check_nvlink`。本讲钻进那个 C++ 检查的内部。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/nvlink.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc) | 探测的全部实现：dlopen NVML、BDF 匹配、链路扫描、矩阵补全、`check_nvlink` 拒绝消息。 |
| [include/fast_ulysses/nvlink.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/nvlink.hpp) | 两个函数的契约文档：`nvlink_matrix` 返回什么、`nullopt` 意味着什么、为什么不用 `cudaDeviceCanAccessPeer`。 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | `_require_nvlink`：收集全组设备号、共享 GPU 时跳过、把 C++ 的消息变成 `RuntimeError`。 |
| [python/fast_ulysses/cli.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py) | `fast-ulysses doctor` 子命令：打印构建信息、设备表、NVLink 矩阵及其判读文案。 |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | pybind 暴露：`nvlink_matrix`（dict 或 None）与 `check_nvlink`（str）。 |
| [test/distributed/nvlink.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/nvlink.py) | 探测的测试：用 pynvml 写的第二意见做逐对比对，并把 fail-open 行为钉死。 |

## 4. 核心概念与源码讲解

### 4.1 dlopen NVML：让「没有 NVML」变成「答不出」而不是「装不上」

#### 4.1.1 概念说明

NVML 是管理面组件，不是运行面组件——算子本身跑起来不需要它，只有构造期的拓扑检查和 `doctor` 需要。如果编译期链接它，扩展 `.so` 的动态依赖表里就会多出 `libnvidia-ml.so.1`，于是在一台没装（或装的版本太老、缺这几个入口点）的机器上，**扩展本身加载失败**，`import fast_ulysses` 直接报错——一个与用户想做的事毫无关系的死法。

解法是 `dlopen` 运行期加载。文件头注释把这条契约写得很清楚：

> NVML is loaded with dlopen rather than linked, so a machine without it -- or with one too old for these entry points -- reports "cannot say" instead of failing to load the extension.

见 [src/nvlink.cc:1-4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L1-L4)：一台没有 NVML（或 NVML 太老）的机器报告的是「说不出」，而不是扩展加载失败。

#### 4.1.2 核心流程

```text
nvlink()（进程内只执行一次，std::call_once）
  ├─ dlopen("libnvidia-ml.so.1", RTLD_LAZY | RTLD_LOCAL)
  │    失败 → handle = nullptr → 以后所有询问都返回 nullopt
  ├─ dlsym 解析 5 个入口点：
  │    nvmlInit_v2 / nvmlDeviceGetHandleByPciBusId_v2 /
  │    nvmlDeviceGetNvLinkState / nvmlDeviceGetNvLinkRemoteDeviceType /
  │    nvmlDeviceGetNvLinkRemotePciInfo_v2
  ├─ 调 nvmlInit_v2()，非 0 → 同「没有 NVML」处理
  └─ 结果缓存进 static Nvml

nvlink_matrix(devices) 入口
  └─ !lib.ok()（库没开起来或缺任一符号）→ return nullopt   # 「说不出」
```

#### 4.1.3 源码精读

`Nvml` 结构体持有库句柄和五个函数指针，`ok()` 要求全部就位——任何一个符号缺失（老版本 NVML）都算不可用，见 [src/nvlink.cc:43-56](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L43-L56)：`Nvml` 结构体保存 dlopen 句柄与五个入口点，`ok()` 在句柄或任一符号缺失时返回假。

加载与初始化只做一次：

```cpp
lib.handle = dlopen("libnvidia-ml.so.1", RTLD_LAZY | RTLD_LOCAL);
...
if (lib.init == nullptr || lib.init() != kNvmlSuccess) {
    lib.handle = nullptr;  // treat a failed init as "no NVML"
}
```

见 [src/nvlink.cc:66-85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L66-L85)：`nvml()` 用 `std::call_once` 保证整个进程只加载并初始化 NVML 一次（NVML init 幂等但不免费，且这个文件只在组构造和 `doctor` 里被调），初始化失败被等同为「没有 NVML」。

`nvlink_matrix` 的第一道门就是它：

```cpp
const Nvml& lib = nvml();
if (!lib.ok()) {
    return std::nullopt;
}
```

见 [src/nvlink.cc:114-119](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L114-L119)：NVML 不可用时矩阵返回 `nullopt`——这是「无法询问」，与「没有链路」是两种答案。头文件对 `nullopt` 的语义有正式承诺，见 [include/fast_ulysses/nvlink.hpp:18-25](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/nvlink.hpp#L18-L25)：`nullopt` 表示 NVML 不可用或太老，此时调用方没有拒绝这个组的依据，应当说明情况而不是瞎猜。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认扩展 `.so` 对 NVML 没有动态依赖（dlopen 换来的解耦是真实存在的）。
2. **操作步骤**：在装好 fast-ulysses 的环境里运行
   ```bash
   ldd $(python -c "import fast_ulysses._C as c; print(c.__file__)") | grep -i nvml
   ```
   `grep` 无输出（退出码 1）即为预期。
3. **观察现象**：依赖表里有 `libcudart`、`libtorch` 系，唯独没有 `libnvidia-ml`。
4. **预期结果**：无输出。再运行 `ldd ... | grep -c libcudart` 对照，确认不是 ldd 本身失效。
5. 本讲写作环境未安装该扩展，此命令结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把五个 `dlsym` 中的 `nvlinkDeviceGetNvLinkRemotePciInfo_v2` 换成一个老版 NVML 没有的符号（比如带 `_v3` 的假想版本），会发生什么？
**答案**：`dlsym` 返回 `nullptr`，`ok()` 为假，`nvlink_matrix` 对所有输入都返回 `nullopt`，`check_nvlink` 返回空串放行——机器照常建组，只是失去了这层保护。这正是设计意图：太老的 NVML 等价于「说不出」。

**练习 2**：`nvml()` 为什么用 `std::call_once` 而不是每次调用都 dlopen？
**答案**：源码注释给出两个理由：NVML init 幂等但有开销；这个库只在组构造和 `doctor` 里被用到，做一次就够。另外 `static` 缓存让后续调用零成本拿到同一份函数指针表。

**练习 3**：`dlopen` 失败（机器没装驱动管理库）时进程会崩溃吗？
**答案**：不会。`dlopen` 返回 `nullptr` 时 lambda 直接 return，`lib.handle` 保持 `nullptr`，`ok()` 为假，一切走 `nullopt` 路径。`dlopen` 的错误不是异常，只是返回值。

### 4.2 BDF 匹配：把 CUDA 设备号接到 NVML 句柄

#### 4.2.1 概念说明

探测的输入是 **CUDA 设备索引**（`group.py` 收集来的 `cuda:0`、`cuda:3`……），而 NVML 认的是 **PCI 总线号字符串**，链路远端报出来的又是 **PCI 三元组**。所以第一步是造两张查找表：

- `by_bdf`：BDF → CUDA 索引。扫描链路时，远端的 PCI 信息靠它翻译回「这是组里的哪块卡」。
- `handles`：CUDA 索引 → NVML 设备句柄。扫描链路时靠它对每块卡发起询问。

#### 4.2.2 核心流程

```text
对 devices 里的每个索引 i：
  ├─ cudaGetDeviceProperties(i) → 取 pciDomainID / pciBusID / pciDeviceID
  │    失败 → cudaGetLastError() 清掉挂起的错误码 → 整个矩阵 nullopt
  ├─ by_bdf[{domain,bus,device}] = i
  ├─ snprintf "%08x:%02x:%02x.0" → NVML 总线号字符串
  └─ nvmlDeviceGetHandleByPciBusId_v2(bus_id) → handles[i]
       失败 → 整个矩阵 nullopt
```

注意：这里任何一步失败都让**整个矩阵**作废（返回 `nullopt`），而不是把这块卡当成「没有链路」——「没有链路」是一个肯定的答案，绝不能用「没问成」冒充。

#### 4.2.3 源码精读

CUDA 侧的 BDF 三元组来自设备属性，见 [src/nvlink.cc:96-110](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L96-L110)：`device_bdf` 从 `cudaGetDeviceProperties` 取 domain/bus/device；失败时先 `cudaGetLastError()` 把错误**消费掉**再返回假——注释解释了为什么：这个文件回答的是「这些设备能否互通」，一块问不出来的设备本身就是一个答案、不是失败，但 CUDA 的错误码是粘性的，不清掉它会从调用方之后的某处不相干代码里冒出来。

两张表的构建：

```cpp
char bus_id[32];
std::snprintf(bus_id, sizeof(bus_id), "%08x:%02x:%02x.0", bdf.domain, bdf.bus, bdf.device);
void* h = nullptr;
if (lib.handle_by_pci_bus_id(bus_id, &h) != kNvmlSuccess) {
    return std::nullopt;
}
```

见 [src/nvlink.cc:121-136](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L121-L136)：先把每块卡记进 `by_bdf`，再按 NVML 的 `%08x:%02x:%02x.0` 格式拿句柄，两步任一失败整个矩阵作废。`Bdf` 结构体与它的字典序比较见 [src/nvlink.cc:88-94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L88-L94)：`std::tie` 实现三元组字典序，让它能当 `std::map` 的键。

对 NVML 报回的远端 PCI 信息，只读 domain/bus/device 三个字段，且只在远端已自报是 GPU 之后才读——`NvmlPciInfo` 结构体的注释说明了为什么这么保守，见 [src/nvlink.cc:30-41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L30-L41)：一个匹配不上的结果被报告为「无法判定」而不是「链路缺失」，这样布局变化不会变成一次错误的拒绝。

#### 4.2.4 代码实践

1. **实践目标**：验证 `snprintf` 格式串与 `nvidia-smi` 报的 PCI 信息一致。
2. **操作步骤**：在有 GPU 的机器上运行
   ```bash
   nvidia-smi --query-gpu=index,pci.domain,pci.bus,pci.device --format=csv
   ```
   取 GPU 0 的三个数，手工代入 `"%08x:%02x:%02x.0"` 格式化一遍（例如 domain=0、bus=0、device=0 → `00000000:00:00.0`）。
3. **观察现象**：与 `nvidia-smi --query-gpu=pci.bus_id` 的输出（形如 `00000000:41:00.0`）逐字符对照。
4. **预期结果**：两串完全一致；不一致则本探测在这台机器上会整体退化为 `nullopt`（fail-open 放行）。
5. 本讲写作环境无 GPU，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`cudaGetDeviceProperties` 失败后为什么必须调 `cudaGetLastError()`？
**答案**：CUDA API 的错误是粘性的：不取走它会一直挂着，从调用方之后**任何**下一条 CUDA API 的返回值里冒出来，出错位置离这里十万八千里。探测把它消费掉，让「这块卡问不出来」干净地变成整个矩阵的 `nullopt`。

**练习 2**：为什么 `by_bdf` 匹配不上远端时只是「不标记」，而不是报错？
**答案**：远端可能是组外的一块 GPU（比如 8 卡机上只建了一个 2 卡子组），链路通向组外不构成关于组内任何一对的证据。把它当错误会让合法配置被拒；当「没链路」又证据不足；所以只能不置位。真正的防线是「问都问不成 → nullopt」。

**练习 3**：`handles` 与 `by_bdf` 为什么是两张表而不是一张？
**答案**：两张表的键值方向不同：扫描时对**本组设备**发问用 `handles[i]`（按 CUDA 索引），翻译**链路远端**用 `by_bdf`（按 PCI 三元组查回 CUDA 索引）。一张双向 map 也能写，但两个方向各自一张 `std::map` 更直白。

### 4.3 两种拓扑：直连 GPU-GPU 链路与 NVSwitch 补全

#### 4.3.1 概念说明

矩阵 `linked` 要回答的是「每对**有序**设备 (i, j) 是否 NVLink 相连」。证据有两种：

1. **直连**：i 的某条激活链路的远端类型是 GPU，且远端 PCI 恰好是 j → `linked[{i,j}] = linked[{j,i}] = true`。
2. **NVSwitch**：i 的某条激活链路通向交换芯片。NVML 问不到「交换芯片连着谁」，但拓扑事实是**每节点一块 NVSwitch fabric，挂上去的 GPU 两两互通**——所以只要 i 和 j 都有通向交换芯片的激活链路，它们就是相连的。

初始化时对角线为真、其余为假，见 [src/nvlink.cc:138-143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L138-L143)：`linked` 先填上 `(i==j)`，之后只把有证据的格子翻成 true。

#### 4.3.2 核心流程

```text
on_switch = ∅
对 handles 里的每块卡 entry（每块卡各带一份 answered = false）：
  对 link = 0..31：
    rc = nvlink_state(handle, link, &active)
    rc == NOT_SUPPORTED 或 INVALID_ARGUMENT → answered = true，跳出   # 设备答完：「没有更多链路」
    rc != SUCCESS                → 跳出（answered 保持当前值）         # 真的问不成
    answered = true
    active == 0                  → continue（链路在但未激活）
    remote_type(handle, link) == SWITCH → on_switch 加入本卡
    remote_type(handle, link) == GPU    → remote_pci 查远端 BDF
                                          → 在 by_bdf 里命中 j → linked[i,j]=linked[j,i]=true
  若 !answered → 整个矩阵 nullopt        # 这块卡一行一列全是「未问」，与真没链路不可区分

# NVSwitch 补全：单节点单 fabric，挂上去的两两互通
对 on_switch 里的每对 (i, j)：linked[{i,j}] = true
```

关键分类学——NVML 返回码的三种含义（[src/nvlink.cc:20-28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L20-L28) 定义了这些常量并写明分类理由）：

| 返回码 | 含义 | 处理 |
| --- | --- | --- |
| `NVML_SUCCESS`（0） | 问到了 | 看 `active`，继续问下一条 |
| `NOT_SUPPORTED`（3） | 设备压根没有 NVLink，链路 0 就这么答 | **这是回答**：`answered = true`，结束扫描 |
| `INVALID_ARGUMENT`（2） | 链路号越过最后一条（有 NVLink 的设备越界时这么答） | **这也是回答**：`answered = true`，结束扫描 |
| 其他 | 真的问不成 | 跳出，`answered` 保持原值 |

扫描上限 `kMaxLinks = 32` 刻意高过当下 NVML 的 `NVML_NVLINK_MAX_LINKS`（18），因为终止条件是「设备答完」而不是某个魔法数，见 [src/nvlink.cc:26](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L26)。

#### 4.3.3 源码精读

每设备一份的 `answered` 是本讲最容易被「简化」掉的正确性细节：

```cpp
// Per DEVICE, not once for the whole sweep. Shared, the first device to answer would let
// every later one that NVML refused keep the `false` it was initialised with above ...
bool answered = false;
```

见 [src/nvlink.cc:145-151](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L145-L151)：`answered` 声明在**按设备的循环体内**。注释推演了共享版本的 bug：若整个扫描共享一个标志，第一块答上来的卡把标志置真之后，后面任何一块被 NVML 拒答的卡都会带着「初始化时的全 false 矩阵行」蒙混过关，`check_nvlink` 于是以「缺一条链路」为由拒绝这个组——而真相是**根本没人问成功**。头文件承诺的恰恰是相反的行为。

状态扫描与两种远端类型的处理见 [src/nvlink.cc:152-183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L152-L183)：`NOT_SUPPORTED`/`INVALID_ARGUMENT` 都记为「设备答完」（源码注释：两者都是设备在回答——「没有更多链路了」——绝不能与 NVML 不可用混淆）；未激活的链路 `continue` 跳过；远端是交换芯片就进 `on_switch` 集合，是 GPU 就查远端 PCI 并在 `by_bdf` 里对回组内设备，命中则双向置真。

未答设备的熔断见 [src/nvlink.cc:185-189](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L185-L189)：一块问不出来的卡让整个矩阵作废——它的一行一列会读作「未连接」，与真的没有链路**不可区分**，所以只能整体返回 `nullopt`。

NVSwitch 补全见 [src/nvlink.cc:192-197](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L192-L197)：源码注释写明依据——**每节点一块 fabric**，所以每块有交换链路的 GPU 都能到达其他每一块。这是把一条拓扑常识编码成了探测的一部分。

#### 4.3.4 代码实践

1. **实践目标**：在纸上跑通扫描循环，掌握返回码分类。
2. **操作步骤**：假设设备 0 有 4 条链路——link 0 远端是 SWITCH 且激活、link 1 远端是 GPU 且 PCI 等于设备 3、link 2 激活但远端类型查询返回非 SUCCESS、link 3 `active == 0`；设备 3 的链路 0 返回 `NOT_SUPPORTED`。手工模拟 [src/nvlink.cc:145-190](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L145-L190) 的循环。
3. **观察现象**：设备 0 的 `answered` 在 link 0 成功时已置真；设备 3 在链路 0 处「答完」。
4. **预期结果**：`on_switch = {0}`，`linked[{0,3}]` 与 `linked[{3,0}]` 保持 false（PCI 匹配只发生在 link 1 的方向，且它指向设备 3？——不，link 1 的远端是 GPU 且 PCI 等于设备 3，所以 `linked[{0,3}]=linked[{3,0}]=true`！）；由于设备 3 不在 `on_switch` 里，交换补全不改变任何格子。最终矩阵：0-3 互通（靠 link 1 的直连证据），对角线为真。如果你第一步就答错了，重读 4.3.2 的流程图。
5. 纸面推演可自行核对的测试对照物：[test/distributed/nvlink.py:110-117](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/nvlink.py#L110-L117) 是同一套返回码分类在 pynvml 参照实现里的镜像。

#### 4.3.5 小练习与答案

**练习 1**：一台 8 卡 NVSwitch 机器上，`on_switch` 会有几个元素？矩阵里几个格子为真？
**答案**：8 个元素（每卡至少一条激活的交换链路）。矩阵是 8×8 全真（64 个格子），其中对角线本来初始化为真，补全循环把其余 56 个也置真。

**练习 2**：为什么「远端类型查询失败」只是 `continue`，而「链路状态问不出」可能让整个矩阵作废？
**答案**：`nvlink_state` 是扫描的**地基**——它答不上来就无从知道这台设备答没答过（`answered` 悬置），一行一列变「未知」，必须熔断成 `nullopt`。而 `remote_type`/`remote_pci` 失败只是**这一条链路的证据**缺失，丢掉它最多样本里少一对 true，不会伪造证据。测试的参照实现在这点上刻意更严格（任何 per-link 错误都返回 None），见 [test/distributed/nvlink.py:32-39](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/nvlink.py#L32-L39) 里写明的「刻意不对称」。

**练习 3**：`kMaxLinks` 为什么定 32 而不是正好 18？
**答案**：18 是「今天 NVML 的上限」，硬编码它意味着新硬件出更多链路时扫描提前截断、后面的链路全漏。32 只是「足够大的安全上限」，真正的终止条件是设备自己用 `INVALID_ARGUMENT` 报「到头了」。

### 4.4 check_nvlink 与 fail-open：「说不出」既不是「都连着」也不是「拒绝」

#### 4.4.1 概念说明

`nvlink_matrix` 有三种可能的输出，语义各不相同：

| 输出 | 语义 | `check_nvlink` 的动作 |
| --- | --- | --- |
| 矩阵，全对为 true | 回答了：都连着 | 空串（无异议） |
| 矩阵，含 false 对 | 回答了：有不连的对 | 拒绝消息（点名第一对） |
| `nullopt` | **说不出** | 空串（放行，不警告） |

第三行就是 fail-open：探测失效时的代价是「一台 PCIe 机器上的组建起来了，跑得慢」；而 fail-closed 的代价是「一台没装 NVML 的 NVLink 机器上永远建不了组」。项目选了前者，并在 [docs/api.md:30-34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L30-L34) 里向用户写明了后果：NVML 缺失或某台设备探不出时，组建起来、无拒绝也无警告，这样的机器上 PCIe 组会慢慢地跑而不是在这里被抓住。

头文件同样把这条语义写成 `check_nvlink` 契约的一部分，见 [include/fast_ulysses/nvlink.hpp:27-29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/nvlink.hpp#L27-L29)：每对都连、**或 NVML 说不出**时返回空串；否则返回点名第一对未连设备的消息。

#### 4.4.2 核心流程

```text
check_nvlink(devices):
  matrix = nvlink_matrix(devices)
  !matrix → return ""            # fail-open：说不出 ≠ 拒绝
  扫描有序对 (i, j)，i != j：
    matrix[{i,j}] == false → return 拒绝消息（点名 cuda:i 与 cuda:j，
                              解释为何慢、给出 require_nvlink=False 与 doctor 两条出路）
  return ""

Python 侧（group.py::_require_nvlink，先于上面的调用）：
  all_gather 本 rank 的设备号 → 全组得到同一份 devices 列表
  devices 有重复（两 rank 共享一 GPU）→ 直接 return（跳过检查）
  problem = _C.check_nvlink(devices)
  problem 非空 → raise RuntimeError(problem)
```

Python 侧先做 `all_gather` 还有一层意义：每个 rank 拿到**同一份设备列表**，跑**同一段确定性代码**，得到**同一个答案**——拒绝是全组一起发生的（所有 rank 同时 raise），不会有 rank 挂在后续集合操作里等一个已经死掉的同伴。这与 u2-l1 讲过的「检查必须集体一致」一脉相承。

#### 4.4.3 源码精读

拒绝消息的构造见 [src/nvlink.cc:201-220](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L201-L220)：`check_nvlink` 对 `nullopt` 返回空串（第一处 fail-open 出口），随后找出第一对未连的设备，返回的消息包含四件事——点名 `cuda:i and cuda:j`、解释原因（fast-ulysses 直写对端内存，走 PCIe——尤其跨 CPU socket——比 torch.distributed 还慢，因为后者会绕开这条链路走）、给出路（这个组用 torch.distributed，或传 `require_nvlink=False` 去测量）、指路（`fast-ulysses doctor` 打印完整矩阵）。

Python 侧的入口与跳过逻辑见 [python/fast_ulysses/group.py:98-109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L98-L109)：docstring 写明设计意图——每个 rank 检查**同一份**设备集合，答案全组一致，拒绝在实践中是集体的；`len(set(devices)) != len(devices)` 时直接 return，注释说明「每 GPU 多于一个 rank」不是这个检查所针对的拓扑。构造函数里它在一切分配之前被调用，见 [python/fast_ulysses/group.py:77-78](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L77-L78)。

fail-open 不是口头承诺，而是被测试**钉死**的行为：

```python
if _C.nvlink_matrix([999]) is not None:
    out.append("nvlink_matrix over a device that cannot be probed answered instead of None")
admitted = _C.check_nvlink([0, 999])
if admitted != "":
    out.append(f"check_nvlink over a device that cannot be probed refused: {admitted!r}")
```

见 [test/distributed/nvlink.py:196-208](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/nvlink.py#L196-L208)：`fail_open_pin` 用一个探测不了的设备号（999）断言两件事——矩阵必须是 `None` 而不是编造出来的矩阵；`check_nvlink` 对它必须返回空串放行。docstring 写明钉死的目的：将来谁把它改成 fail-closed 或编造矩阵，都会是一次**被注意到的变更**。真正的拒绝只能来自「回答了的矩阵里含有 false 对」。

pybind 侧的暴露见 [src/bindings.cc:786-799](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L786-L799)：`nvlink_matrix` 把 `nullopt` 翻译成 Python 的 `None`、矩阵翻译成以 `(i, j)` 元组为键的 dict；`check_nvlink` 原样返回字符串。这两个是 pybind 函数而不是 torch 算子——它们进不了 schema（u6-l1 讲过什么样的东西才当算子）。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到 fail-open 的两条出口。
2. **操作步骤**：在**没有 GPU** 但装好了 fast-ulysses（扩展能加载，`libcudart` 没有 GPU 也能链接）的环境运行：
   ```bash
   python -c "from fast_ulysses import _C; print(_C.nvlink_matrix([0])); print(repr(_C.check_nvlink([0, 1])))"
   ```
3. **观察现象**：`cudaGetDeviceProperties(0)` 失败 → `device_bdf` 返回假 → 整个矩阵 `nullopt`。
4. **预期结果**：第一行打印 `None`，第二行打印 `''`（空串）——设备都不存在，检查依然「放行」。这正是「说不出 ≠ 拒绝」。
5. 本讲写作环境未安装该扩展，**待本地验证**。若你的环境装了 pynvml，可加跑 [test/distributed/nvlink.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/nvlink.py) 的对照逻辑（它按 `torchrun --nproc_per_node=8` 设计，无多卡时会报 BLIND 并由 [test/test_distributed.py:71-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L71-L76) 转成 pytest skip）。

#### 4.4.5 小练习与答案

**练习 1**：NVML 完全不可用时，`UlyssesGroup(require_nvlink=True)` 构造会怎样？
**答案**：正常建组。`check_nvlink` 拿到 `nullopt` 返回空串，Python 侧 `problem` 为空、不 raise，构造继续走对称内存注册。代价是失去这层保护（api.md 明说不保证能抓住 PCIe 组）。

**练习 2**：两个 rank 共享一块 GPU（`CUDA_VISIBLE_DEVICES` 配置成同一块卡跑两进程）时检查为什么跳过？
**答案**：`all_gather` 收回的设备列表有重复，[group.py:105-106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L105-L106) 的 `len(set(devices)) != len(devices)` 命中后直接 return。这个探测回答的是「GPU 与 GPU 之间的链路类型」，一块卡内部没有「对间链路」可言，这不是它针对的拓扑。

**练习 3**：为什么 `cudaDeviceCanAccessPeer` 不能替代这个探测？
**答案**：头文件的文件级注释就是答案，见 [include/fast_ulysses/nvlink.hpp:1-8](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/nvlink.hpp#L1-L8)：它只报告「P2P 可用」，而 **PCIe 上 P2P 同样可用**；只有 NVML 能报出链路的**类型**。本检查存在的理由恰恰是「PCIe 上这个算子正确但比 torch.distributed 慢」（design.md「Why NVLink only」一节，[docs/design.md:160-175](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L160-L175)：实测两台 PCIe 节点 0.62× 与 0.14× 基线，差 5 倍且不稳定），所以必须区分「能 P2P」与「P2P 走的是 NVLink」。

### 4.5 doctor 的矩阵输出与判读

#### 4.5.1 概念说明

`fast-ulysses doctor` 是把这套探测单独拿出来跑的入口：不建进程组、不构造 `UlyssesGroup`，对**所有可见设备**（`list(range(n))`，不是某个组的设备）打一遍矩阵。cli.py 的模块 docstring 事先声明了它的地位，见 [python/fast_ulysses/cli.py:1-13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L1-L13)：绿矩阵是**必要条件**而非替代——构造函数检查的是真实组里的真实设备，doctor 检查的是整机。

#### 4.5.2 核心流程

```text
_doctor():
  扩展加载失败            → "extension: FAILED TO LOAD"，退出码 1
  torch.cuda 不可用        → "devices: none visible to CUDA"，退出码 1
  打印构建信息（build_meta）与设备表（sm 架构不在构建列表里的标 <-- NOT IN THIS BUILD）
  matrix = _C.nvlink_matrix(list(range(n)))
  matrix is None          → 打印 "NVML could not report the link topology..."，退出码 0   # fail-open 可见化
  打印矩阵：对角线 " . "、连通 " y "、不通 " N "，收集 missing 对
  missing 非空            → 打印被点名对数与例子，退出码 1
  n < 2                   → 打印「单设备无对可查」的说明句，退出码 0
  否则                    → "all n devices are mutually NVLink-joined"，退出码 0
```

#### 4.5.3 源码精读

探测调用与 fail-open 的可见化见 [python/fast_ulysses/cli.py:72-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L72-L76)：`matrix is None` 时打印两行——「NVML 在这台机器上说不出链路拓扑」「UlyssesGroup 反正会建，这里没法说它该不该」——然后**退出码 0**。注意这里把「说不出」原样告诉用户而不是渲染成全 N 矩阵，与构造函数的 fail-open 保持同一套语言。

矩阵的渲染见 [python/fast_ulysses/cli.py:78-88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L78-L88)：表头 `nvlink, row=from col=to`，格子三态——对角线 ` . `、连通 ` y `、不通 ` N `；不通的对同时收进 `missing`。

三种结论的文案见 [python/fast_ulysses/cli.py:90-103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L90-L103)：有 missing 对时打印数量与前四个例子并退出码 1；单设备时打印一句「没有对可查、也没有超过 world_size 1 可跑」退出码 0（注释说明：pair 循环此时为空，「1 块设备全互连」是读者唯一会带走的话，所以专门写了这句话）；全连通打印 `all n devices are mutually NVLink-joined`。

设备表部分（矩阵之前）见 [python/fast_ulysses/cli.py:57-70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L57-L70)：每块卡打印名字、`sm_架构`、显存，运行架构不在构建架构列表（`build_info()["cuda_arch_list"]`，只取前导数字，`90a` 与 `90` 视作同一批 GPU）里的会标 ` <-- NOT IN THIS BUILD`。

#### 4.5.4 代码实践

1. **实践目标**：读懂 doctor 输出的每一块。
2. **操作步骤**：运行 `fast-ulysses doctor`（等价入口：`python -m fast_ulysses.cli doctor` 不存在——cli 靠 console script 暴露；从仓库树运行可用 `python python/fast_ulysses/cli.py doctor`，前提是包已装好可导入）。
3. **观察现象**：依次是扩展路径 → 构建键值对 → 设备表 → 矩阵（或「说不出」两行）→ 结论行；退出码用 `echo $?` 查看。
4. **预期结果**：NVLink 整机上矩阵除对角线外全 `y`、退出码 0；PCIe 混插机器上出现 `N` 格、退出码 1；无 NVML 机器上打「说不出」两行、退出码 0。
5. 本讲写作环境无 GPU 与已装扩展，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：doctor 打出全 `y` 矩阵、退出码 0，能否保证随后 `UlyssesGroup()` 一定成功？
**答案**：不能完全保证。doctor 查的是整机所有可见设备；构造函数查的是**那个组实际用到的设备**（且是集合意义上的同一份列表）。cli.py 的 docstring 明说绿矩阵是必要条件不是替代。此外构造是集合操作（对称内存 bootstrap），失败模式不止拓扑一种。

**练习 2**：doctor 对「NVML 说不出」为什么用退出码 0 而不是 1？
**答案**：与 `check_nvlink` 的 fail-open 对齐：「说不出」不是故障判定。退出码 1 只留给**有答案且答案是坏**（存在 `N` 对）或干脆没有 CUDA 设备。用户看到的两行文案把「说不出」明明白白讲出来，不是静默。

**练习 3**：`missing[:4]` 这个切片在文案里起什么作用？
**答案**：8 卡机器上缺失对可能很多，全打出来淹没读者；打印总数加前四个例子足以定位拓扑问题，消息保持一行级别。

## 5. 综合实践

本实践把整讲串起来：跑 doctor、与 `nvidia-smi topo -m` 交叉验证、回答三个设计问题。

**步骤**（需要一台有 GPU 的机器；没有的话做纸面版，见后）：

1. 运行 `fast-ulysses doctor`，保存完整输出。
2. 运行 `nvidia-smi topo -m`，保存输出与图例（Legend）。
3. 对照判读，写一段话（5-8 句）：
   - topo 矩阵中标记为 `NV#`（如 `NV4`、`NV18`，表示经 NVLink 相连、`#` 为链路数；NVSwitch 机型上表现为每卡相同的较大数字）的格子，在 doctor 矩阵里应为 `y`；
   - topo 中标记为 `PIX` / `PXB` / `PHB` / `NODE` / `SYS`（各种 PCIe 路径）的格子，在 doctor 里应为 `N`；
   - 两个矩阵都是对称的；对角线 doctor 用 ` . `、topo 用 `X`；
   - 若有不一致（topo 说 NV 而 doctor 说 N，或反过来），那是本探测的 bug 候选——[test/distributed/nvlink.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/nvlink.py) 的三层测试就是为抓这种不一致而生的（第一层合成负控制校准比对器、第二层 pynvml 第二意见逐对比对、第三层验证构造函数按答案行动，见其文件头 [test/distributed/nvlink.py:1-40](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/nvlink.py#L1-L40)）。
4. 回答三个问题（每题三行以内，答案可对照 4.4.5）：
   - **NVML 完全不可用时构造函数会怎样？**——`check_nvlink` 返回空串，组照常建立，无拒绝无警告（fail-open；docs/api.md 明示）。
   - **两 rank 共享一 GPU 时检查为何跳过？**——`all_gather` 收回的设备列表有重复，`group.py` 直接 return；「一块卡内部」没有对间链路可言，不是这个检查针对的拓扑。
   - **为什么 `cudaDeviceCanAccessPeer` 不能替代？**——它只说「P2P 可用」，PCIe 上同样为真；只有 NVML 能报**链路类型**，而这个检查存在的全部理由就是区分 NVLink 与 PCIe（后者上本传输正确但慢于 torch.distributed）。

**纸面版（无 GPU）**：通读 [python/fast_ulysses/cli.py:38-103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L38-L103) 的 `_doctor`，为 `n=3`、`matrix = {(0,1):True,(1,0):True,(0,2):False,(2,0):False,(1,2):True,(2,1):True, ...}` 手工写出 doctor 应打印的每一行与退出码，再对照代码逐行核验（预期：矩阵里 0-2 行有 `N`，退出码 1，`missing` 为 `[(0,2),(2,0)]`，文案打印「2 pair(s), e.g. [(0, 2), (2, 0)]」）。同样标注「待本地验证」直到真正在有卡机器上跑过。

## 6. 本讲小结

- **dlopen NVML**：五个入口点运行期解析，库缺失/太老/init 失败一律归为「说不出」（`nullopt`），绝不升级成扩展加载失败。
- **BDF 匹配**：CUDA 设备号 ↔ PCI 三元组 ↔ NVML 句柄两张查找表；任何一步问不成，整个矩阵作废——「没问成」永远不能冒充「没链路」。
- **两种拓扑**：直连靠远端 PCI 对回组内设备；NVSwitch 靠「单节点单 fabric」的拓扑事实做全互连补全；`NOT_SUPPORTED` 与 `INVALID_ARGUMENT` 都是设备在**回答**「没有更多链路」。
- **每设备一份 `answered`**：防止「没人问成功」被误读成「链路缺失」而错误拒绝；一块未答设备熔断整个矩阵，因为它的一行一列与真缺失不可区分。
- **fail-open**：`nullopt` → `check_nvlink` 返回空串放行；真正的拒绝只来自「回答了的矩阵里的 false 对」；Python 侧 `all_gather` 保证全组答案一致、拒绝集体发生，两 rank 共享一 GPU 时跳过。
- **doctor**：整机级矩阵 + 三态格子（` . ` / ` y ` / ` N`）+ 三种结论文案，「说不出」单独可见化且退出码 0；绿矩阵是必要条件不是构造检查的替代。

## 7. 下一步学习建议

本讲是 u8「拓扑探测、构建与发布工程」的第一篇。接下来：

- **u8-l2（构建系统）**：看 `CMakeLists.txt` 如何探测 `c10d::register_work` 符号（与本章「dlopen 解耦依赖」相对照的另一种符号探测哲学——`nm` 实测导出表）以及 `FAST_ULYSSES_CUDA_ARCH` 如何决定 doctor 设备表里那行 `cuda_arch_list`。
- 回顾 **u2-l1**：本讲钻进去的 `check_nvlink` 正是那里 `_require_nvlink` 的被调方；现在你应该能完整复述从 `all_gather` 到拒绝消息的每一环。
- 若对「为什么 PCIe 上慢」感兴趣，直接读 [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) 的「Why NVLink only」与 [docs/benchmark.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md) 的「Why not PCIe」——那是对本讲拒绝消息里「slower than torch.distributed」一句的完整实测依据。
- 想看测试侧的纵深，可提前读 [test/distributed/nvlink.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/nvlink.py) 全文（u9-l1、u9-l3 会系统讲对抗性测试与负控制的方法论，这里是其中一个实例）。
