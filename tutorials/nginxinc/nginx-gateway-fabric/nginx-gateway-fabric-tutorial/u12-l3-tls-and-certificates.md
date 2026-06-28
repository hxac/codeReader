# TLS 安全：前端证书、后端 TLS 与 Agent mTLS

## 1. 本讲目标

NGINX Gateway Fabric（NGF）的整条数据链路上有三段加密通道，它们由三套完全不同的机制负责。本讲读完之后，你应该能够：

- 说出**前端 TLS**（客户端 → NGINX 数据面）是如何用 Gateway Listener 的证书引用、`SSLKeyPair` 与 `ssl_certificate` 指令实现的，并能区分它附带的前端 mTLS 客户端校验。
- 读懂 **BackendTLSPolicy**（NGINX → 后端 Service）从 CRD 到图中 `VerifyTLS`，再到 NGINX `proxy_ssl_*` 指令的完整翻译链路，包括 `CACertificateRefs` 与 `WellKnownCACertificates` 两条互斥路径。
- 理解**控制面 ↔ 数据面 Agent mTLS** 由谁签发证书、谁做服务端/客户端、控制面如何强制校验客户端、以及为什么能不停机轮换证书。
- 把一条 CA bundle 从用户写的 ConfigMap/Secret 一路追踪到数据面 Pod 里的 `/etc/nginx/conf.d/secrets/*.crt` 文件与 `proxy_ssl_trusted_certificate` 指令。

本讲属于专家层，默认你已经读过 u5-l3（后端解析）、u2-l3（证书生成命令）、u5-l4（`dataplane.Configuration`）与 u7-l1（gRPC Agent 通信），并掌握 Source+Valid+Conditions 节点三元组、`ReferenceGrant`、配置按 dest 分桶合并等概念。

## 2. 前置知识

### 2.1 TLS 里的三类角色

在 NGINX 的语境里，一次 TLS 握手有明确的「服务端」和「客户端」：

- **服务端**出示证书（`ssl_certificate` + `ssl_certificate_key`），证明「我是谁」。
- **客户端**如果想被服务端校验，就出示一张客户端证书。是否要求客户端证书，由服务端的 `ssl_verify_client` 决定。
- **CA 证书**（`*_trusted_certificate`）是信任根：握手的对端证书必须能被这一组 CA 验证通过。

NGF 的三段通道，服务端/客户端各不相同，这是本讲最容易混淆的点，先用一张表钉死：

| 通道 | 服务端 | 客户端 | 用的信任根 / 证书 |
|------|--------|--------|-------------------|
| 前端 TLS | NGINX 数据面 | 外部客户端（浏览器/调用方） | Listener 引用的 TLS Secret；可选前端 mTLS 用 `CACertificateRefs` |
| 后端 TLS | 后端 Service | NGINX 数据面 | BackendTLSPolicy 的 `CACertificateRefs`（ConfigMap/Secret）或系统 CA |
| Agent mTLS | 控制面 gRPC server | 数据面 NGINX Agent | `generate-certs` 自签的 CA，控制面强制校验客户端 |

### 2.2 三类证书材料的区分

承接 u5-l4，NGF 把所有证书材料严格分成两类，落盘方式不同、用途不同：

- **`SSLKeyPair`**（含私钥）：服务端用来「出示身份」的证书+私钥对，文件后缀 `.pem`，用于前端 TLS 终止、Gateway 自身后端 TLS 客户端证书。
- **`CertBundle`**（仅 CA，无私钥）：用来「验证对端」的信任根，文件后缀 `.crt`，用于后端 mTLS、前端客户端校验、外部鉴权/JWT/OIDC。

这两类各有独立 ID 体系（`SSLKeyPairID` / `CertBundleID`），且 ID 本身就是文件名。本讲会反复用到这条规则：**出示身份 → 私钥对（`.pem`）；验证对端 → CA bundle（`.crt`）**。

### 2.3 一个常量：Alpine 系统 CA

当 BackendTLSPolicy 选「用系统信任的 CA」时，NGF 不会下发任何证书文件，而是直接指向数据面镜像（基于 Alpine）自带的系统 CA 包。这个路径是写死的常量：

[internal/controller/state/dataplane/configuration.go:33](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L33)

```go
AlpineSSLRootCAPath = "/etc/ssl/cert.pem"
```

记住这个路径，后面会看到它怎么被塞进 `proxy_ssl_trusted_certificate` 指令。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [cmd/gateway/certs.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go) | `generate-certs` 子命令的证书签发实现：自签 CA、签服务端/客户端证书、写两个共享 CA 的 Secret |
| [cmd/gateway/commands.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go) | `generate-certs` 子命令装配（flag、SAN 域名） |
| [internal/controller/nginx/agent/grpc/grpc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go) | 控制面 gRPC 服务端：构建 mTLS 凭据、强制校验客户端、动态重载证书 |
| [internal/controller/nginx/agent/grpc/interceptor/interceptor.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/interceptor/interceptor.go) | Agent mTLS 之上的第二道闸门：TokenReview 校验 Agent 的 ServiceAccount |
| [internal/controller/state/graph/backend_tls_policy.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_tls_policy.go) | 图阶段：解析、校验 BackendTLSPolicy，确定有效 CA 引用 |
| [internal/controller/state/graph/backend_refs.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go) | 把 BackendTLSPolicy 关联到具体 BackendRef |
| [internal/controller/state/graph/gateway_listener.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go) | 前端 TLS：解析 Listener 的证书引用、前端 mTLS 校验模式 |
| [internal/controller/state/dataplane/configuration.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go) | 渲染层：构建 `SSLKeyPair` / `CertBundle` / `VerifyTLS` / `SSL` |
| [internal/controller/nginx/config/servers.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go) | 把 `VerifyTLS` 翻译成 http `proxy_ssl_*` 指令结构 |
| [internal/controller/nginx/config/servers_template.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go) | http `proxy_ssl_*` 指令模板 |
| [internal/controller/nginx/config/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go) | 把 `SSLKeyPair` / `CertBundle` 写成 `.pem` / `.crt` 文件 |
| [examples/secure-backend/backendtlspolicy.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/secure-backend/backendtlspolicy.yaml) | BackendTLSPolicy 示例 |

## 4. 核心概念与源码讲解

### 4.1 前端 TLS：NGINX 如何向外部客户端出示证书

#### 4.1.1 概念说明

「前端 TLS」指的是外部客户端（浏览器、调用方）连到 NGINX 数据面时的那段加密。这部分由 Gateway API 的标准字段驱动：

- HTTPS/TLS Listener 在 `tls.certificateRefs` 里引用一个 `Secret`（`kubernetes.io/tls` 类型，含 `tls.crt` + `tls.key`）。
- 跨命名空间引用要走 `ReferenceGrant` 授权（承接 u5-l3）。
- NGINX 用 `ssl_certificate` / `ssl_certificate_key` 出示证书；一个 Listener 可引用多张证书，靠 **SNI**（Server Name Indication）在握手时按客户端请求的域名选证书。

这里还藏着一个**附加能力**：前端 mTLS，即 NGINX 反过来校验客户端证书。它由 Gateway API 实验性字段 `gateway.spec.tls.frontend`（含 `CACertificateRefs` 与 `Mode`）驱动，NGF 用 `ssl_client_certificate` + `ssl_verify_client` 实现。

另外还有一个容易混淆的「Gateway 自身后端 TLS 客户端证书」：`gateway.spec.tls.backend.clientCertificateRef`。它是 NGINX 连接上游时**主动出示**给后端的客户端证书（即让后端能反向校验 NGINX），属于私钥对，落盘成 `.pem`。注意它和 BackendTLSPolicy 是相反方向——BackendTLSPolicy 决定 NGINX 如何「验证后端」，而 backend client cert 决定后端如何「验证 NGINX」。

#### 4.1.2 核心流程

前端 TLS 的处理在「图构建」与「配置渲染」两个阶段完成：

```text
图阶段 (gateway_listener.go)
  Listener.tls.certificateRefs
    → createExternalReferencesForTLSSecretsResolver 校验+ReferenceGrant
    → Listener.ResolvedSecrets []types.NamespacedName   # 解析出的合法证书 Secret 列表
  gateway.spec.tls.frontend (可选前端 mTLS)
    → createFrontendTLSCaCertReferenceResolver
    → Listener.ValidationMode / Listener.CACertificateRefs
  gateway.spec.tls.backend (可选 Gateway 后端 client cert)
    → gateway.SecretRef

渲染阶段 (configuration.go)
  Listener.ResolvedSecrets / gateway.SecretRef
    → buildSSLKeyPairs → SSLKeyPair{Cert, Key}            # .pem 文件
  Listener.ResolvedSecrets
    → buildSSL → SSL{KeyPairIDs, Protocols, Ciphers, ...} # ssl_certificate / SNI
  gateway.spec.tls.frontend
    → buildFrontendTLSCertBundles + addClientSettingsToSSLServers  # .crt + ssl_client_certificate
```

#### 4.1.3 源码精读

**(1) 图阶段：解析证书引用，结果存进 Listener**

Listener 的结构里同时挂着「出示身份」与「校验客户端」两组字段：

[internal/controller/state/graph/gateway_listener.go:60-66](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L60-L66)

```go
// ValidationMode holds the TLS validation configuration for the listener.
ValidationMode v1.FrontendValidationModeType
// CACertificateRefs holds the resolved CA certificate references for the listener.
CACertificateRefs []v1.ObjectReference
// ResolvedSecrets is the list of namespaced names of the Secrets resolved for this listener.
// Only applicable for HTTPS listeners. Supports multiple certificates for SNI-based selection.
ResolvedSecrets []types.NamespacedName
```

`ResolvedSecrets` 是前端 TLS 终止的核心：把 `tls.certificateRefs` 里合法的 Secret 全部解析进来，支持多个以实现 SNI。

前端 mTLS 的解析由 `createFrontendTLSCaCertReferenceResolver` 负责。它先读 `gateway.spec.tls.frontend`，按 `perPort` 优先、否则用 `default`，取出 `CACertificateRefs` 与 `Mode`，然后调用 `validateFrontendTLS` 做校验+跨命名空间授权：

[internal/controller/state/graph/gateway_listener.go:979-1035](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L979-L1035)

```go
frontend := gw.Source.Spec.TLS.Frontend
for i, port := range frontend.PerPort {
    if port.TLS.Validation == nil || len(port.TLS.Validation.CACertificateRefs) == 0 {
        continue
    }
    if port.Port == l.Source.Port {
        caCertRefs = port.TLS.Validation.CACertificateRefs
        validationMode = port.TLS.Validation.Mode
        ...
    }
}
...
l.ValidationMode = validationMode
l.CACertificateRefs = caCertRefs

if l.ValidationMode == v1.AllowInsecureFallback {
    msg := "Validation Mode: AllowInsecureFallback is set for at least one listener"
    gw.Conditions = append(gw.Conditions, conditions.NewGatewayInsecureFrontendValidationMode(msg))
}
```

两种模式的区别（对应 NGINX 的 `ssl_verify_client`）：

- **`AllowValidOnly`**（默认）：必须有被 CA 信任的客户端证书，否则拒绝。→ `ssl_verify_client on`。
- **`AllowInsecureFallback`**：请求客户端证书但不强制、也不做 CA 校验，可当作「尽力而为」识别身份。→ `ssl_verify_client optional_no_ca`，并会打一条 `InsecureFrontendValidationMode` 警告条件。

Gateway 自身后端客户端证书则在 gateway.go 解析为 `SecretRef`：

[internal/controller/state/graph/gateway.go:33-34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L33-L34)

```go
// SecretRef is the namespaced name of the secret referenced by the Gateway for backend TLS.
SecretRef *types.NamespacedName
```

它由 `gw.Spec.TLS.Backend.ClientCertificateRef` 经 `validateGatewayTLSBackend` 校验后填入。

**(2) 渲染阶段：私钥对 → SSLKeyPair，落盘成 `.pem`**

`buildSSLKeyPairs` 把合法的证书 Secret 转成 `SSLKeyPair`：

[internal/controller/state/dataplane/configuration.go:489-524](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L489-L524)

```go
func buildSSLKeyPairs(secretsMap map[types.NamespacedName]*secrets.Secret, gateway *graph.Gateway) map[SSLKeyPairID]SSLKeyPair {
    keyPairs := make(map[SSLKeyPairID]SSLKeyPair)
    for _, l := range gateway.Listeners {
        if l.Valid && len(l.ResolvedSecrets) > 0 {
            for _, secretNsName := range l.ResolvedSecrets {
                id := generateSSLKeyPairID(secretNsName)
                secret := secretsMap[secretNsName]
                if secret != nil && secret.CertBundle != nil {
                    keyPairs[id] = SSLKeyPair{
                        Cert: secret.CertBundle.Cert.TLSCert,
                        Key:  secret.CertBundle.Cert.TLSPrivateKey,
                    }
                }
            }
        }
    }
    if gateway.Valid && gateway.SecretRef != nil { /* gateway 后端 client cert 同样落为 keypair */ }
    return keyPairs
}
```

注意 `id` 由 `generateSSLKeyPairID` 生成，就是文件名 `ssl_keypair_<ns>_<name>`，最终在生成器里加后缀：

[internal/controller/nginx/config/generator.go:241-260](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L241-L260)

```go
func generatePEM(id dataplane.SSLKeyPairID, cert []byte, key []byte) agent.File {
    return agent.File{
        Name:        generatePEMFileName(id),
        Permissions: file.SecretFileMode,
        // PEM 内容 = 证书 + 私钥
    }
}
func generatePEMFileName(id dataplane.SSLKeyPairID) string {
    return filepath.Join(secretsFolder, string(id)+".pem")
}
```

`secretsFolder` 即 `/etc/nginx/conf.d/secrets`，且权限是更严格的 `SecretFileMode`（承接 u6-l1：私钥类文件绕过模板链直接 append、权限更严）。

**(3) 出示证书：buildSSL → ssl_certificate 与 SNI**

`buildSSL` 把 Listener 的解析结果转成 `SSL` 结构，含一组 `KeyPairIDs`（多张证书 → SNI），并从 `tls.options` 读取协议、密码套件：

[internal/controller/state/dataplane/configuration.go:1655-1678](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L1655-L1678)

```go
func buildSSL(listener *graph.Listener) *SSL {
    keyPairIDs := make([]SSLKeyPairID, 0, len(listener.ResolvedSecrets))
    for _, secretNsName := range listener.ResolvedSecrets {
        keyPairIDs = append(keyPairIDs, generateSSLKeyPairID(secretNsName))
    }
    ssl := &SSL{KeyPairIDs: keyPairIDs}
    if listener.Source.TLS != nil && listener.Source.TLS.Options != nil {
        if protocols, ok := listener.Source.TLS.Options[graph.SSLProtocolsKey]; ok { ssl.Protocols = string(protocols) }
        if ciphers, ok := listener.Source.TLS.Options[graph.SSLCiphersKey]; ok   { ssl.Ciphers = string(ciphers) }
        ...
    }
    return ssl
}
```

这里把 K8s 资源视图（Listener）切成了 NGINX 配置视图（`ssl_certificate ssl_keypair_xxx.pem; ssl_protocols ...;`），是 u5-l4「渲染模型」思想的具体体现。

**(4) 前端 mTLS：CA bundle → ssl_client_certificate + ssl_verify_client**

前端客户端校验的 CA 同样是「需求驱动」过滤。`buildFrontendTLSCertBundles` 为每个启用前端校验的 HTTPS Listener 生成一个唯一的 `CertBundleID`，并把对应 CA bundle 内容收集起来：

[internal/controller/state/dataplane/configuration.go:568-622](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L568-L622)

```go
for _, listener := range gateway.Listeners {
    if listener.Source.Protocol != v1.HTTPSProtocolType { continue }
    if len(listener.CACertificateRefs) == 0 { continue }
    caCertRef := types.NamespacedName{Namespace: gateway.Source.Namespace,
        Name: fmt.Sprintf("%s_%d", gateway.Source.Name, listener.Source.Port)}
    id := generateCertBundleID(caCertRef)
    clientSettingsMap[listener.Source.Port] = listenerClientSettings{CertBundleID: id, validationMode: listener.ValidationMode}
    if listener.ValidationMode != v1.AllowInsecureFallback {
        bundles = getFrontendTLSCertBundles(id, bundles, gateway, refCertBundleIndex, listener.CACertificateRefs)
    }
}
addClientSettingsToSSLServers(sslServers, clientSettingsMap)
```

注意 `AllowInsecureFallback` 模式**不配置任何 CA bundle**（因为不校验）。`addClientSettingsToSSLServers` 再把校验模式写进每个 SSL server：

[internal/controller/state/dataplane/configuration.go:692-716](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L692-L716)

```go
switch clientSettings.validationMode {
case v1.AllowInsecureFallback:
    sslServers[i].SSL.ClientCertBundleID = ""
    sslServers[i].SSL.VerifyClient = SSLVerifyClientOptionalNoCA   // optional_no_ca
    sslServers[i].SSL.RequireVerifiedCert = false
default: // AllowValidOnly
    sslServers[i].SSL.ClientCertBundleID = clientSettings.CertBundleID
    sslServers[i].SSL.VerifyClient = SSLVerifyClientOn             // on
    sslServers[i].SSL.RequireVerifiedCert = true
}
```

这两个枚举就是 NGINX 指令 `ssl_verify_client on` 与 `optional_no_ca`（见 `SSLVerifyClientMode` 定义）。模板侧则是 `ssl_client_certificate <bundle路径>;`：

[internal/controller/nginx/config/servers_template.go:32-34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L32-L34)

```text
{{- if $s.SSL.ClientCertificate }}
    ssl_client_certificate {{ $s.SSL.ClientCertificate }};
{{- end }}
```

#### 4.1.4 代码实践

**实践目标**：观察前端 TLS 私钥对如何从 Secret 变成数据面 Pod 里的 `.pem` 文件，并理解 SNI 多证书。

**操作步骤**（基于 u1 的本地 kind 集群）：

1. 部署一个带 TLS 证书的 cafe 类示例（参考 `examples/`），其 Gateway Listener 的 `tls.certificateRefs` 指向一个含 `tls.crt`/`tls.key` 的 Secret。
2. 找到数据面 NGINX Pod：
   ```bash
   kubectl -n nginx-gateway get pods -l app.kubernetes.io/name=nginx
   ```
3. 进入 Pod 查看前端证书文件（路径前缀 `/etc/nginx/conf.d/secrets/`）：
   ```bash
   kubectl -n nginx-gateway exec <nginx-pod> -- ls /etc/nginx/conf.d/secrets/
   kubectl -n nginx-gateway exec <nginx-pod> -- grep -rl "ssl_certificate" /etc/nginx/conf.d/
   ```
4. 用 `openssl s_client` 验证 SNI 选证书：
   ```bash
   kubectl -n nginx-gateway port-forward <nginx-pod> 8443:443 &
   openssl s_client -connect 127.0.0.1:8443 -servername cafe.example.com -showcerts </dev/null 2>/dev/null | openssl x509 -noout -subject
   ```

**需要观察的现象**：

- `secrets/` 目录下有形如 `ssl_keypair_<ns>_<secret>.pem` 的文件，内容是证书+私钥拼接。
- HTTP 配置里出现 `ssl_certificate /etc/nginx/conf.d/secrets/ssl_keypair_xxx.pem;`。
- 若 Listener 引用了多张证书，会看到多条 `ssl_certificate` 指令，分别对应不同 SNI 主机名。

**预期结果**：`openssl` 输出的证书 subject 与你创建的 TLS Secret 中的证书一致；切换 `-servername` 会得到不同证书（若配置了多张）。

> 若本地未起 kind 集群，以上为「待本地验证」；可改为源码阅读型：在 `configuration.go:489` 的 `buildSSLKeyPairs` 打断点或加日志，确认遍历 `listener.ResolvedSecrets` 生成的 ID 与最终 `.pem` 文件名一致。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 HTTPS Listener 没有任何证书能通过校验（`ResolvedSecrets` 为空），NGF 会怎么处理？

**参考答案**：该 Listener 被标记为 `Valid=false`（见 `gateway_listener.go` 中 `if len(l.ResolvedSecrets) == 0 { l.Valid = false }`），不会为其生成 server 块，并把错误沉淀为 Listener 的 Conditions（如 `ResolvedRefs=False`）。对应渲染层 `buildSSLKeyPairs` 也会跳过无效 Listener。

**练习 2**：前端 mTLS 的两种模式 `AllowValidOnly` 与 `AllowInsecureFallback` 分别映射到 NGINX 的哪条指令值？为什么后者会打一条警告条件？

**参考答案**：`AllowValidOnly` → `ssl_verify_client on`（强制 CA 校验，无证书/不可信即拒绝）；`AllowInsecureFallback` → `ssl_verify_client optional_no_ca`（不强制、不做 CA 校验）。后者等于放弃了真正的身份校验，安全性弱，故 NGF 用 `NewGatewayInsecureFrontendValidationMode` 在 Gateway status 上显式告警，提醒运维这是「尽力而为」模式。

---

### 4.2 BackendTLSPolicy：NGINX 如何校验后端 Service 的证书

#### 4.2.1 概念说明

「后端 TLS」指 NGINX 作为客户端，用 HTTPS/`proxy_pass https://` 连接后端 Service 时如何校验后端证书。这部分由 Gateway API 标准的 **BackendTLSPolicy** CRD 驱动（注意：它是标准 CRD，不是 NGF 自有 CRD），通过 `targetRefs` 附着到一个 Service。

一个 BackendTLSPolicy 关心三件事：

1. **校验谁**：`targetRefs` 指向某个 Service（可选 `sectionName` 限定到某端口）。
2. **用谁的 CA 校验**：`validation.caCertificateRefs`（用户自管的 ConfigMap/Secret）**或** `validation.wellKnownCACertificates: System`（用系统信任的 CA）。这两者**互斥**。
3. **校验哪个主机名**：`validation.hostname`，必须与后端证书里的 SAN/CN 一致，对应 NGINX 的 `proxy_ssl_name`。

回顾方向：4.1 讲的是「NGINX 出示证书给客户端」，本节是「NGINX 校验后端的证书」。所以这里需要的是**信任根（CA bundle）**而非私钥对，落盘成 `.crt`。

#### 4.2.2 核心流程

```text
图阶段
  processBackendTLSPolicies
    → validateBackendTLSPolicy
        ├─ hostname 校验
        └─ 互斥分支：
           ├─ caCertificateRefs(1) → validateBackendTLSCACertRef(Kind=ConfigMap|Secret, group=""|"core", 能解析)
           └─ wellKnownCACertificates → 必须 System
    → BackendTLSPolicy{Valid, CaCertRef, Conditions}
  findBackendTLSPolicyForService
    → 按 targetRef.name (+sectionName port) 匹配 BackendRef，冲突按创建时间裁决
    → BackendRef.BackendTLSPolicy

渲染阶段
  convertBackendTLS(btp, gwNsName)
    → CaCertRef.Name != "" : VerifyTLS{CertBundleID=cert_bundle_<ns>_<name>, Hostname}
      CaCertRef.Name == "" (System): VerifyTLS{RootCAPath=AlpineSSLRootCAPath, Hostname}
  buildCertBundles  → 仅保留被 backend/tlsServer/extAuth 引用的 CA bundle
  generator         → 写 cert_bundle_<ns>_<name>.crt 到 /etc/nginx/conf.d/secrets/

模板阶段
  createProxySSLVerify(http) / buildStreamProxySSLVerify(stream)
    → ProxySSLVerify{TrustedCertificate, Name}
  servers_template.go
    → proxy_ssl_server_name on; proxy_ssl_verify on; proxy_ssl_verify_depth 4;
       proxy_ssl_name <hostname>; proxy_ssl_trusted_certificate <path>;
```

#### 4.2.3 源码精读

**(1) 图中的 BackendTLSPolicy 节点**

图中 BackendTLSPolicy 节点沿用 Source+Valid+Conditions 三元组，额外记录 CA 引用与生效的 Gateway：

[internal/controller/state/graph/backend_tls_policy.go:18-34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_tls_policy.go#L18-L34)

```go
type BackendTLSPolicy struct {
    Source *v1.BackendTLSPolicy
    // CaCertRef is the name of the ConfigMap that contains the CA certificate.
    CaCertRef types.NamespacedName
    Gateways []types.NamespacedName
    Conditions []conditions.Condition
    Valid bool
    IsReferenced bool
    Ignored bool
}
```

注意 `CaCertRef` 的注释仍写 ConfigMap，但实际（见下文）允许 Secret 或 ConfigMap 两种。

**(2) 校验：hostname + 互斥的 CA 来源**

`processBackendTLSPolicies` 逐个策略调用 `validateBackendTLSPolicy`。后者把校验结果沉淀进 Conditions 而非抛 error，符合图构建的惯例：

[internal/controller/state/graph/backend_tls_policy.go:68-116](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_tls_policy.go#L68-L116)

```go
if err := validateBackendTLSHostname(backendTLSPolicy); err != nil {
    valid = false
    conds = append(conds, conditions.NewPolicyInvalid(...))
}
switch {
case len(caCertRefs) > 0 && wellKnownCerts != nil:
    valid = false  // 互斥
case len(caCertRefs) > 0:
    // 用用户 CA，需逐条校验
case wellKnownCerts != nil:
    // 必须是 System
default:
    valid = false  // 二者都没有
}
```

CA 引用的细校验在 `validateBackendTLSCACertRef`：**恰好 1 个**引用、Kind 只能是 `ConfigMap` 或 `Secret`、group 只能是 `""` 或 `"core"`，并通过 `resourceResolver.Resolve` 确认资源真实存在：

[internal/controller/state/graph/backend_tls_policy.go:129-174](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_tls_policy.go#L129-L174)

```go
if len(btp.Spec.Validation.CACertificateRefs) != 1 {
    return []conditions.Condition{conditions.NewPolicyInvalid(...TooMany...)}
}
allowedCaCertKinds := []v1.Kind{"ConfigMap", "Secret"}
if !slices.Contains(allowedCaCertKinds, selectedCertRef.Kind) { ... NotSupported ... }
if selectedCertRef.Group != "" && selectedCertRef.Group != "core" { ... NotSupported ... }
if err := resourceResolver.Resolve(resolver.ResourceType(selectedCertRef.Kind), nsName); err != nil { ... }
```

合法时回写 `NewBackendTLSPolicyResolvedRefs`（条件类型见 conditions.go）；非法时回写 `NewBackendTLSPolicyNoValidCACertificate` / `InvalidCACertificateRef` / `InvalidKind` 之一。

「System」分支则要求 `WellKnownCACertificates` 的值严格等于 `WellKnownCACertificatesSystem`：

[internal/controller/state/graph/backend_tls_policy.go:176-186](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_tls_policy.go#L176-L186)

**(3) 关联到 BackendRef：按端口匹配、冲突裁决**

`findBackendTLSPolicyForService` 把策略绑到具体后端。它按 `targetRef.name` + 命名空间匹配，若 `sectionName` 存在还要匹配具体 Service 端口；多个策略命中同一 Service 时，用 `sort.LessClientObject`（按创建时间/资源版本）裁决，败者被打 `PolicyConflicted`：

[internal/controller/state/graph/backend_refs.go:447-510](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L447-L510)

```go
for _, btp := range backendTLSPolicies {
    for _, targetRef := range btp.Source.Spec.TargetRefs {
        if string(targetRef.Name) == refName && btpNs == refNs {
            if targetRef.SectionName != nil {
                // 进一步匹配具体端口
            }
            if beTLSPolicy == nil { beTLSPolicy = btp
            } else if sort.LessClientObject(btp.Source, beTLSPolicy.Source) {
                conflictingPolicies = append(conflictingPolicies, beTLSPolicy); beTLSPolicy = btp
            } else {
                conflictingPolicies = append(conflictingPolicies, btp)
            }
        }
    }
}
```

关联结果挂到 `BackendRef.BackendTLSPolicy`（见 backend_refs.go:28）。

**(4) 渲染：转成 VerifyTLS，区分用户 CA 与系统 CA**

`convertBackendTLS` 是把图节点翻译成渲染模型的关键。它根据 `CaCertRef.Name` 是否为空决定走哪条路径：

[internal/controller/state/dataplane/configuration.go:1328-1345](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L1328-L1345)

```go
func convertBackendTLS(btp *graph.BackendTLSPolicy, gwNsName types.NamespacedName) *VerifyTLS {
    if btp == nil || !btp.Valid { return nil }
    if !slices.Contains(btp.Gateways, gwNsName) { return nil }   // 只对该策略生效的 Gateway 才返回
    verify := &VerifyTLS{}
    if btp.CaCertRef.Name != "" {
        verify.CertBundleID = generateCertBundleID(btp.CaCertRef)   // 用户 CA → 下发 .crt 文件
    } else {
        verify.RootCAPath = AlpineSSLRootCAPath                     // System → 用镜像自带 /etc/ssl/cert.pem
    }
    verify.Hostname = string(btp.Source.Spec.Validation.Hostname)
    return verify
}
```

注意第二个守卫 `!slices.Contains(btp.Gateways, gwNsName)`：一个策略只在它「生效的 Gateway」上才产生校验，这和 4.2.3(2) 里 `addGatewaysForBackendTLSPolicies` 收集的 `Gateways` 列表呼应（受 ancestor 上限约束）。

`VerifyTLS` 的结构很简单，只有三个字段：

[internal/controller/state/dataplane/types.go:671-676](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L671-L676)

```go
type VerifyTLS struct {
    CertBundleID CertBundleID
    Hostname     string
    RootCAPath   string
}
```

`CertBundleID` 与 `RootCAPath` 二者择一：前者表示「下发一份 CA bundle 文件」，后者表示「用某个绝对路径的现成 CA」。

**(5) 需求驱动的 CA bundle 落盘**

CA bundle 只有被引用才会落盘。`buildCertBundles` 扫描所有 backend / TLS server / external-auth，收集真正用到的 `CertBundleID` 集合，再从 `refCertBundles`（Secret+ConfigMap）里挑出对应的写入：

[internal/controller/state/dataplane/configuration.go:739-780](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L739-L780)

```go
referenced := make(map[CertBundleID]struct{}, len(extAuthCertBundleIDs))
for _, bg := range backendGroups {
    for _, b := range bg.Backends {
        if !b.Valid || b.VerifyTLS == nil { continue }
        referenced[b.VerifyTLS.CertBundleID] = struct{}{}
    }
}
for _, s := range tlsServers {
    if s.VerifyTLS == nil { continue }
    referenced[s.VerifyTLS.CertBundleID] = struct{}{}
}
for _, bundle := range refCertBundles {
    id := generateCertBundleID(bundle.Name)
    if _, exists := referenced[id]; exists {
        bundles[id] = getCertRefBundleData(bundle)
    }
}
```

`refCertBundles` 由 `buildRefCertificateBundles` 从被引用的 Secret 与 CaCertConfigMap 汇总（configuration.go:718-737）。最后生成器把它写成 `cert_bundle_<ns>_<name>.crt`：

[internal/controller/nginx/config/generator.go:262-275](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L262-L275)

```go
func generateCertBundle(id dataplane.CertBundleID, cert []byte) agent.File {
    return agent.File{
        Name:        generateCertBundleFileName(id),
        Permissions: file.SecretFileMode,
        ...
    }
}
func generateCertBundleFileName(id dataplane.CertBundleID) string {
    return filepath.Join(secretsFolder, string(id)+".crt")
}
```

注意 `getCertRefBundleData`（configuration.go:782）会尝试 base64 解码失败则按明文处理，兼容两种存放方式。

**(6) 模板：proxy_ssl_* 指令**

http 与 stream 两条路径都有 `VerifyTLS → ProxySSLVerify` 的转换，逻辑对称——「有 CertBundleID 用下发文件路径，否则用 RootCAPath」：

[internal/controller/nginx/config/servers.go:1694-1708](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1694-L1708)

```go
func createProxySSLVerify(v *dataplane.VerifyTLS) *http.ProxySSLVerify {
    if v == nil { return nil }
    var trustedCert string
    if v.CertBundleID != "" {
        trustedCert = generateCertBundleFileName(v.CertBundleID)   // /etc/nginx/conf.d/secrets/cert_bundle_xxx.crt
    } else {
        trustedCert = v.RootCAPath                                  // /etc/ssl/cert.pem
    }
    return &http.ProxySSLVerify{TrustedCertificate: trustedCert, Name: v.Hostname}
}
```

stream 侧 `buildStreamProxySSLVerify`（stream_servers.go:316）结构相同。模板渲染出的指令如下：

[internal/controller/nginx/config/servers_template.go:310-319](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L310-L319)

```text
{{- if $l.ProxySSLVerify }}
    {{ $proxyOrGRPC }}_ssl_server_name on;
    {{ $proxyOrGRPC }}_ssl_verify on;
    {{ $proxyOrGRPC }}_ssl_verify_depth 4;
    {{- if $l.ProxySSLVerify.Name}}
    {{ $proxyOrGRPC }}_ssl_name {{ $l.ProxySSLVerify.Name }};
    {{- end }}
    {{- if $l.ProxySSLVerify.TrustedCertificate }}
    {{ $proxyOrGRPC }}_ssl_trusted_certificate {{ $l.ProxySSLVerify.TrustedCertificate }};
    {{- end }}
{{- end }}
```

`$proxyOrGRPC` 对 HTTPRoute 取 `proxy`、对 GRPCRoute 取 `grpc`，所以 gRPC 后端会生成 `grpc_ssl_*` 系列指令。

#### 4.2.4 代码实践

**实践目标**：为 HTTPS 后端配置 BackendTLSPolicy，追踪 CA bundle 从 ConfigMap/Secret 到 NGINX `proxy_ssl_trusted_certificate` 的完整链路（本讲的主实践）。

**操作步骤**：

1. 参考 `examples/secure-backend/`，先准备一个自带证书的后端（示例用 cert-manager 签发，证书 SAN 为 `secure-app.example.com`），并把签发出来的 CA 放进一个 Secret `backend-cert`：

   ```yaml
   # 节选自 examples/secure-backend/backendtlspolicy.yaml
   apiVersion: gateway.networking.k8s.io/v1
   kind: BackendTLSPolicy
   metadata:
     name: backend-tls
   spec:
     targetRefs:
     - group: ''
       kind: Service
       name: secure-app
     validation:
       caCertificateRefs:
       - name: backend-cert
         group: ''
         kind: Secret
       hostname: secure-app.example.com
   ```

2. 在 HTTPRoute 里把 `backendRefs` 指向 `secure-app`，路径用 `https://`（即 backendRefs 需要 NGF 走 TLS 连后端，配合 BackendTLSPolicy 生效）。
3. `kubectl apply` 后查看策略状态：
   ```bash
   kubectl get backendtlspolicy backend-tls -o yaml | grep -A6 conditions
   ```
   预期出现 `Accepted=True` 与 `ResolvedRefs=True`（即 `NewBackendTLSPolicyResolvedRefs`）。
4. 进入数据面 Pod，确认 CA bundle 已落盘并被引用：
   ```bash
   NGINX_POD=$(kubectl -n nginx-gateway get pod -l app.kubernetes.io/name=nginx -o name | head -1)
   kubectl -n nginx-gateway exec $NGINX_POD -- ls /etc/nginx/conf.d/secrets/ | grep cert_bundle
   kubectl -n nginx-gateway exec $NGINX_POD -- grep -R "proxy_ssl_" /etc/nginx/conf.d/
   ```

**需要观察的现象**：

- `secrets/` 下出现 `cert_bundle_<ns>_backend-cert.crt`（用 `generateCertBundleID` 推算的文件名）。
- 生成的 location 里出现：
  ```
  proxy_ssl_server_name on;
  proxy_ssl_verify on;
  proxy_ssl_verify_depth 4;
  proxy_ssl_name secure-app.example.com;
  proxy_ssl_trusted_certificate /etc/nginx/conf.d/secrets/cert_bundle_<ns>_backend-cert.crt;
  ```

**预期结果**：用 `curl` 经 Gateway 访问该路由，能成功拿到后端响应（说明 CA 校验通过）。若故意把 CA Secret 换成错误的 CA，`ResolvedRefs` 会变 False、或 NGINX 返回 502（后端证书无法被信任）。

> 若本地无法运行 cert-manager，可改用任意自签 CA 填入 Secret。若完全无集群，请改为源码阅读型：在 `convertBackendTLS`（configuration.go:1328）确认 `CaCertRef.Name != ""` 走 `CertBundleID` 分支，再在 `buildCertBundles`（configuration.go:739）确认该 ID 进入 `referenced` 集合，最后在 `servers_template.go:310` 确认指令模板——三处连起来就是 CA bundle 进入 NGINX 配置的完整路径。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `wellKnownCACertificates: System` 和 `caCertificateRefs` 同时写进同一个 BackendTLSPolicy，会发生什么？

**参考答案**：两者互斥。`validateBackendTLSPolicy` 的 `switch` 会命中 `len(caCertRefs) > 0 && wellKnownCerts != nil` 分支，置 `Valid=false` 并追加 `NewPolicyInvalid("CACertificateRefs and WellKnownCACertificates are mutually exclusive")`（backend_tls_policy.go:84-88）。策略不会生效，也不会下发任何校验配置。

**练习 2**：BackendTLSPolicy 选 `System` 时，数据面会出现 `.crt` 文件吗？为什么？

**参考答案**：不会。`convertBackendTLS` 中 `CaCertRef.Name` 为空，走 `RootCAPath = AlpineSSLRootCAPath`（`/etc/ssl/cert.pem`）分支，`CertBundleID` 为空；`createProxySSLVerify` 因此用 `RootCAPath` 作为 `proxy_ssl_trusted_certificate` 的值，指向镜像自带的系统 CA 包，NGF 不下发任何 CA 文件。

**练习 3**：为什么 `convertBackendTLS` 要检查 `slices.Contains(btp.Gateways, gwNsName)`？

**参考答案**：一个 BackendTLSPolicy 可能因 PolicyAncestorLimit 被限制只对部分 Gateway 生效（见 `addGatewaysForBackendTLSPolicies`）。`btp.Gateways` 记录的是「该策略实际生效的 Gateway 列表」。只有当前正在构建配置的 Gateway 在列表里，才应生成 `VerifyTLS`，否则该 Gateway 对应的数据面不会校验此后端。

---

### 4.3 Agent mTLS：控制面与数据面之间的加密通道

#### 4.3.1 概念说明

控制面（Go）与数据面（NGINX Agent）之间通过 gRPC 长连接通信，用于下发配置、命令等（承接 u7-l1）。这条通道必须加密且双向认证，否则任何能到达控制面 gRPC 端口的 Pod 都能伪造 Agent 身份、注入恶意配置。这就是 **Agent mTLS**：

- **控制面是 gRPC 服务端**，出示 `server-tls` Secret 里的服务端证书。
- **数据面 Agent 是 gRPC 客户端**，出示 `agent-tls` Secret 里的客户端证书。
- 两端共享同一个自签 CA（`ca.crt`），构成共同信任根。
- 控制面强制校验客户端（`RequireAndVerifyClientCert`），即「没有可信客户端证书就拒绝连接」。

这些证书由 `generate-certs` 子命令（承接 u2-l3）在 Helm 的 pre-install/pre-upgrade Job 里自签生成。注意它和前两节完全独立：前端/后端 TLS 的证书由用户提供，而 Agent mTLS 的证书由 NGF 自己签发、自己消费。

此外，NGF 在 mTLS 之上还叠加了**第二道闸门**：TokenReview。Agent 连接时还要带一个它自身 ServiceAccount 的 token，控制面用 Kubernetes TokenReview 校验它确实是期望的、且对应 Pod 已 Running。这让「拿到证书」不足以完成认证，仍需「是正确的 ServiceAccount」。

#### 4.3.2 核心流程

```text
证书生产 (generate-certs Job)
  generateCA()                          → CA 私钥 + 自签 CA 证书 (IsCA, KeyUsageCertSign)
  generateCert(CA, serverDNSNames)      → 服务端证书 (SAN = <svc>.<ns>.svc)
  generateCert(CA, clientDNSNames)      → 客户端证书 (SAN = *.<clusterDomain>)
  createSecrets()                       → server-tls{ca.crt, tls.crt, tls.key}
                                       → agent-tls {ca.crt, tls.crt, tls.key}  (共享同一 ca.crt)

控制面 gRPC 服务端 (Start)
  getTLSConfig()                        → 读 /var/run/secrets/ngf/{ca.crt,tls.crt,tls.key}
  buildTLSCredentials()
    ├─ 启动期校验三个文件可解析
    ├─ MinVersion = TLS 1.3
    └─ GetConfigForClient: 每条新连接重新从磁盘读 CA + 证书 → 轮换免重启
  filewatcher.Watch(三个文件)           → 文件变化 → resetConnChan → 断开旧连接重连

连接鉴权 (interceptor)
  validateConnection → validateToken
    → TokenReview(token, audience)       → 校验是合法 ServiceAccount
    → waitForRunningPod                  → 校验 Agent Pod 已 Running
```

#### 4.3.3 源码精读

**(1) 自签 CA + 服务端/客户端证书**

`generateCertificates` 是入口：先建 CA，再用同一 CA 分别签服务端和客户端证书：

[cmd/gateway/certs.go:48-76](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L48-L76)

```go
func generateCertificates(service, namespace, clientDNSDomain, serverTLSDomain string) (*certificateConfig, error) {
    caCertPEM, caKeyPEM, err := generateCA()
    ...
    serverCert, serverKey, err := generateCert(caKeyPair, serverDNSNames(service, namespace, serverTLSDomain))
    ...
    clientCert, clientKey, err := generateCert(caKeyPair, clientDNSNames(clientDNSDomain))
    ...
}
```

CA 自身是 RSA 2048、有效期 3 年（`expiry = 365 * 3 * 24 * time.Hour`），关键用法位是 `KeyUsageCertSign`（允许签发下级证书）且 `IsCA: true`：

[cmd/gateway/certs.go:78-110](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L78-L110)

```go
ca := &x509.Certificate{
    Subject:  subject,
    NotBefore: time.Now(), NotAfter: time.Now().Add(expiry),
    SubjectKeyId: subjectKeyID(caKey.N),
    KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
    IsCA: true, BasicConstraintsValid: true,
}
```

服务端与客户端证书的区别只在 **SAN（Subject Alternative Name）**：

[cmd/gateway/certs.go:157-167](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L157-L167)

```go
func serverDNSNames(service, namespace, serverTLSDomain string) []string {
    return []string{fmt.Sprintf("%s.%s.%s", service, namespace, serverTLSDomain)} // 如 nginx-gateway.nginx-gateway.svc
}
func clientDNSNames(dnsDomain string) []string {
    return []string{fmt.Sprintf("*.%s", dnsDomain)} // 如 *.cluster.local
}
```

服务端证书的 SAN 是控制面 Service 的集群内 DNS 名（`<service>.<namespace>.svc`，`serverTLSDomain` 默认 `svc`，见 commands.go:747-750）；客户端证书的 SAN 是通配 `*.<clusterDomain>`（默认 `cluster.local`）。

**(2) 写两个共享 CA 的 Secret**

`createSecrets` 把三份材料写进两个 `SecretTypeTLS` Secret，**关键是两者用同一份 `ca.crt`**，这才构成 mTLS 的共同信任根：

[cmd/gateway/certs.go:178-202](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L178-L202)

```go
serverSecret := corev1.Secret{
    Type: corev1.SecretTypeTLS,
    Data: map[string][]byte{
        secrets.CAKey:      certConfig.caCertificate,      // CA
        secrets.TLSCertKey: certConfig.serverCertificate,  // 服务端证书
        secrets.TLSKeyKey:  certConfig.serverKey,          // 服务端私钥
    },
}
clientSecret := corev1.Secret{
    Type: corev1.SecretTypeTLS,
    Data: map[string][]byte{
        secrets.CAKey:      certConfig.caCertificate,      // 同一个 CA
        secrets.TLSCertKey: certConfig.clientCertificate,  // 客户端证书
        secrets.TLSKeyKey:  certConfig.clientKey,          // 客户端私钥
    },
}
```

`CAKey = "ca.crt"`、`TLSCertKey = "tls.crt"`、`TLSKeyKey = "tls.key"` 这三个常量定义在 secrets.go:38-50。`server-tls` 挂给控制面，`agent-tls` 挂给数据面 Agent（两份材料的去向是部署侧的事，证书生成代码只管产出）。

**(3) 控制面 gRPC 服务端：强制校验客户端 + 动态重载**

控制面把三个文件挂载到固定路径（`/var/run/secrets/ngf/`）：

[internal/controller/nginx/agent/grpc/grpc.go:27-33](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L27-L33)

```go
caCertPath   = "/var/run/secrets/ngf/" + secrets.CAKey    // ca.crt
tlsCertPath  = "/var/run/secrets/ngf/" + secrets.TLSCertKey // tls.crt
tlsKeyPath   = "/var/run/secrets/ngf/" + secrets.TLSKeyKey  // tls.key
```

`buildTLSCredentials` 是 mTLS 的核心。它先在启动期校验三个文件可解析（缺失/损坏即启动失败），再构造一个 **每次新连接都从磁盘重读 CA 和证书** 的凭据，从而「证书轮换免重启」：

[internal/controller/nginx/agent/grpc/grpc.go:150-189](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L150-L189)

```go
func buildTLSCredentials(caPath, certPath, keyPath string) (credentials.TransportCredentials, error) {
    // 启动期校验
    if _, err := loadCACertPool(caPath); err != nil { return nil, err }
    if _, err := tls.LoadX509KeyPair(certPath, keyPath); err != nil { return nil, err }
    tlsConfig := &tls.Config{
        GetConfigForClient: buildConfigForClient(caPath, certPath, keyPath),
        MinVersion:         tls.VersionTLS13,
    }
    return credentials.NewTLS(tlsConfig), nil
}

func buildConfigForClient(...) func(*tls.ClientHelloInfo) (*tls.Config, error) {
    return func(_ *tls.ClientHelloInfo) (*tls.Config, error) {
        certPool, err := loadCACertPool(caPath) // 每条新连接重读 CA
        ...
        return &tls.Config{
            GetCertificate: func(_ *tls.ClientHelloInfo) (*tls.Certificate, error) {
                serverCert, err := tls.LoadX509KeyPair(certPath, keyPath) // 每条新连接重读服务端证书
                ...
            },
            ClientAuth: tls.RequireAndVerifyClientCert,  // 强制且校验客户端证书
            ClientCAs:  certPool,                         // 用 CA 池校验客户端证书
            MinVersion: tls.VersionTLS13,
        }, nil
    }
}
```

三个要点：

- `MinVersion: tls.VersionTLS13`：控制面 gRPC 只接受 TLS 1.3。
- `ClientAuth: tls.RequireAndVerifyClientCert`：没有客户端证书或客户端证书不被 CA 信任，连接被拒绝——这是 mTLS「双向」中由服务端强制的那一环。
- `ClientCAs: certPool`：校验客户端用的信任根，正是 `generate-certs` 签出的同一个 CA。
- `GetConfigForClient` 每条新连接重读磁盘：当 Helm pre-upgrade Job 重新生成证书、Secret 滚动更新、文件被替换后，新连接自动用新证书，**无需重启控制面**。

为了让旧连接也能切到新证书，`Start` 还起了一个 filewatcher 监听这三个文件，变化时通过 `resetConnChan` 触发 Command service 重置连接：

[internal/controller/nginx/agent/grpc/grpc.go:96-102](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L96-L102)

```go
tlsFiles := []string{caCertPath, tlsCertPath, tlsKeyPath}
fileWatcher, err := filewatcher.NewFileWatcher(g.logger.WithName("fileWatcher"), tlsFiles, g.resetConnChan)
...
go fileWatcher.Watch(ctx)
```

**(4) 第二道闸门：TokenReview**

mTLS 通过后，gRPC 拦截器（`ContextSetter`）再做一次身份校验。Agent 在连接元数据里带 `uuid` 和 `authorization`（ServiceAccount token），控制面用 TokenReview 验证 token 合法、且用户名是 `system:serviceaccount:<ns>:<name>` 格式：

[internal/controller/nginx/agent/grpc/interceptor/interceptor.go:138-185](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/interceptor/interceptor.go#L138-L185)

```go
tokenReview := &authv1.TokenReview{
    Spec: authv1.TokenReviewSpec{Audiences: []string{c.audience}, Token: grpcInfo.Token},
}
if err := c.k8sClient.Create(createCtx, tokenReview); err != nil { ... }
if !tokenReview.Status.Authenticated {
    return nil, status.Error(codes.Unauthenticated, ...)
}
usernameItems := strings.Split(tokenReview.Status.User.Username, ":")
if len(usernameItems) != 4 || usernameItems[0] != "system" || usernameItems[1] != "serviceaccount" { ... }
saNamespace := usernameItems[2]; saName := usernameItems[3]
// 还要确认该 SA 下有 Running 的 Pod
if err := c.waitForRunningPod(ctx, logger, opts, saNamespace, saName); err != nil { return nil, err }
```

这层校验与 mTLS 互补：mTLS 保证「你持有被信任的客户端证书」，TokenReview 保证「你是预期的 NGF 数据面 ServiceAccount，且 Pod 已就绪」。两者都过，连接才被 `ConnectionsTracker` 记录并 Ready（承接 u7-l1）。`waitForRunningPod` 还专门吸收了一个启动竞态：WAF 等慢启动 sidecar 让 Agent 在 NGF 看到 Pod Running 之前就拨号（见 interceptor.go:29-45 的注释）。

#### 4.3.4 代码实践

**实践目标**：验证 Agent mTLS 双向校验，并观察证书轮换的免重启行为。

**操作步骤**：

1. 在已部署 NGF 的集群里，确认两个 Secret 存在且共享同一 CA：
   ```bash
   kubectl -n nginx-gateway get secret server-tls agent-tls -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.data.ca\.crt}{"\n"}{end}'
   ```
   预期两行的 `ca.crt`（base64）完全一致。
2. 用 openssl 直接连控制面 gRPC 端口（默认在控制面 Service 暴露），不带客户端证书，观察被拒：
   ```bash
   kubectl -n nginx-gateway port-forward svc/nginx-gateway <port>:<agent-grpc-port> &
   openssl s_client -connect 127.0.0.1:<port> </dev/null 2>&1 | grep -i "alert\|verify"
   ```
   预期看到服务端要求并校验客户端证书（`RequireAndVerifyClientCert`），不提供则握手失败。
3. 用 `agent-tls` 的客户端证书重试，应能完成 TLS 握手：
   ```bash
   kubectl -n nginx-gateway get secret agent-tls -o jsonpath='{.data.tls\.crt}' | base64 -d > agent.crt
   kubectl -n nginx-gateway get secret agent-tls -o jsonpath='{.data.tls\.key}' | base64 -d > agent.key
   kubectl -n nginx-gateway get secret agent-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt
   openssl s_client -connect 127.0.0.1:<port> -cert agent.crt -key agent.key -CAfile ca.crt </dev/null 2>&1 | grep "Verify return code"
   ```

**需要观察的现象**：

- 无客户端证书：TLS 握手被服务端拒绝（对应 `RequireAndVerifyClientCert`）。
- 带正确客户端证书：`Verify return code: 0 (ok)`，握手成功。
- （进阶）触发一次证书重新生成（如重新跑 generate-certs Job + 滚动 Secret），filewatcher 检测到文件变化后旧连接被重置、新连接用新证书，控制面 Pod **不重启**。

**预期结果**：第 2 步失败、第 3 步成功，验证了 mTLS 是「双向强制」的；进阶步骤若不便操作，可记为「待本地验证」。

> 纯源码阅读型替代：对照 grpc.go:184 的 `RequireAndVerifyClientCert` 与 certs.go:178-202 两个 Secret 共享 `ca.crt`，回答「为什么控制面能用同一个 CA 既签服务端证书又校验客户端证书」——因为服务端与客户端证书都由这唯一一个 CA 签发，互为可信方。

#### 4.3.5 小练习与答案

**练习 1**：为什么服务端证书的 SAN 是 `<service>.<namespace>.svc`，而客户端证书的 SAN 是 `*.<clusterDomain>`？

**参考答案**：服务端证书由控制面出示，Agent 要用它访问控制面 Service 的集群内 DNS 名（`<service>.<namespace>.svc`），所以 SAN 必须匹配这个名，否则 Agent 侧的 SNI/主机名校验会失败。客户端证书由 Agent 出示给控制面，控制面 `RequireAndVerifyClientCert` 主要校验「是否被 CA 签名」，对客户端 SAN 的具体值要求宽松，故用通配 `*.<clusterDomain>` 覆盖任意 Agent Pod。

**练习 2**：`buildTLSCredentials` 为什么用 `GetConfigForClient` 每条新连接重读磁盘，而不是在启动时读一次？

**参考答案**：为了让证书轮换（如 Helm pre-upgrade Job 重新 `generate-certs` 并滚动 Secret）对**新连接**立即生效，无需重启控制面 Pod。启动期只读一次会缓存旧证书，直到重启才更新。配合 filewatcher 监听文件变化并触发 `resetConnChan`，旧连接也会被重置，从而整体实现免重启轮换。

**练习 3**：mTLS 已经强制校验客户端证书了，为什么还要在拦截器里再做一次 TokenReview？

**参考答案**：二者是不同维度的认证。mTLS 证明「你持有被这个 CA 签发的客户端证书」，但 CA 是 NGF 自签的，任何能拿到 `agent-tls` Secret 的对象都能通过。TokenReview 进一步要求连接方持有**正确的 Kubernetes ServiceAccount token**（且对应 Pod 已 Running），把身份收敛到「NGF 自己部署的数据面 Agent」，避免证书泄露后被其他工作负载冒充。这是纵深防御。

## 5. 综合实践

把三段 TLS 通道串起来，构造一个「全链路加密」的场景，并对照源码解释每一跳用的证书来自哪里：

1. **前端 TLS（客户端 → NGINX）**：创建一个 HTTPS Listener，`tls.certificateRefs` 引用一个自签证书的 Secret（SAN 为 `app.example.com`）。部署后用 `curl --resolve app.example.com:443:<GW_IP> https://app.example.com` 验证前端证书。
2. **后端 TLS（NGINX → 后端）**：让 HTTPRoute 的后端是一个 HTTPS 服务，并创建 BackendTLSPolicy，`caCertificateRefs` 指向存放后端 CA 的 Secret，`hostname` 与后端证书 SAN 一致。验证 `proxy_ssl_*` 指令出现在数据面配置里（4.2.4 的步骤 4）。
3. **Agent mTLS（控制面 ↔ 数据面）**：确认 `server-tls`/`agent-tls` 共享同一 `ca.crt`（4.3.4 的步骤 1），并用 openssl 验证不带客户端证书被拒（4.3.4 的步骤 2）。

完成后，画一张图，标注三段通道各自：

- 谁是服务端、谁是客户端；
- 出示身份用的是哪个 `SSLKeyPair` / 哪份证书；
- 验证对端用的是哪个 `CertBundle` / CA 来源（用户 Secret、System、还是 generate-certs 的 CA）；
- 对应落盘的文件名（`ssl_keypair_*.pem`、`cert_bundle_*.crt`、`/var/run/secrets/ngf/*`）。

这张图能帮你彻底分清本讲三个模块的边界。注意：前端/后端 TLS 的证书材料由用户提供并经图校验，落盘在 `/etc/nginx/conf.d/secrets/`；Agent mTLS 的证书由 NGF 自签，落盘在 `/var/run/secrets/ngf/`——两条路径互不相干，是本讲最容易混淆之处。

## 6. 本讲小结

- NGF 数据链路有三段独立的 TLS 通道：**前端 TLS**（客户端→NGINX）、**后端 TLS**（NGINX→Service）、**Agent mTLS**（控制面↔数据面），三套证书来源、校验方、落盘位置各不相同。
- **前端 TLS**：图阶段把 `tls.certificateRefs` 解析为 `Listener.ResolvedSecrets`，渲染阶段 `buildSSLKeyPairs` 产出 `SSLKeyPair`（含私钥，落盘 `.pem`），`buildSSL` 用 `KeyPairIDs` 实现 SNI；前端 mTLS 由 `gateway.spec.tls.frontend` 驱动，`AllowValidOnly`→`ssl_verify_client on`、`AllowInsecureFallback`→`optional_no_ca` 并告警。
- **BackendTLSPolicy**：标准 CRD，校验时 `caCertificateRefs` 与 `wellKnownCACertificates: System` 互斥；前者经 `convertBackendTLS` 产出 `CertBundleID`（下发 `cert_bundle_*.crt`），后者产出 `RootCAPath=/etc/ssl/cert.pem`；最终模板生成 `proxy_ssl_verify on; proxy_ssl_name; proxy_ssl_trusted_certificate;`。
- **Agent mTLS**：`generate-certs` 自签一份 CA，签出服务端证书（SAN `<svc>.<ns>.svc`）与客户端证书（SAN `*.<cluster.local>`），写入共享 `ca.crt` 的 `server-tls`/`agent-tls`；控制面 gRPC 用 `RequireAndVerifyClientCert`+`ClientCAs`+TLS 1.3 强制双向校验，`GetConfigForClient` 每连接重读磁盘实现免重启轮换，filewatcher 触发旧连接重置。
- **证书材料二分法**贯穿全讲：出示身份→`SSLKeyPair`（`.pem`，含私钥）；验证对端→`CertBundle`（`.crt`，仅 CA）。这是理解 NGF 所有 TLS 配置生成的主线。
- Agent mTLS 之上还叠加 TokenReview 作为第二道闸门，把身份收敛到正确的 NGF 数据面 ServiceAccount，体现纵深防御。

## 7. 下一步学习建议

- 若想看 BackendTLSPolicy 在 TLSRoute（L4 stream）路径上的表现，复习 **u6-l4（Stream 配置）** 与本讲 4.2.3(6) 的 `buildStreamProxySSLVerify`，对比 http `proxy_ssl_*` 与 stream `proxy_ssl_*` 的对称性。
- 若对「证书如何下发到数据面 Pod 并触发 reload」感兴趣，继续读 **u7-l1/u7-l2（gRPC Agent 通信与文件下发）**，本讲的 `cert_bundle_*.crt`/`ssl_keypair_*.pem` 正是经那条链路送达的 `[]agent.File`。
- 若想动手扩展，参考 **u8-l3（策略到 NGINX 指令的生成）** 与 **u13-l3（二次开发）**，尝试新增一种自定义 TLS 校验来源（如 OCSP、CRL），体会从 CRD→图→渲染模型→模板的完整改动链路；其中 CRL 已有现成实现可参考（`generateCRLBundleID`、`IsCRLBundle`）。
- 建议通读 `internal/controller/state/graph/gateway_listener.go` 的 `validateFrontendTLS` 与 `internal/controller/state/graph/backend_tls_policy.go` 全文，把本讲省略的 ancestor 上限、ReferenceGrant 跨命名空间授权等边界补全。
