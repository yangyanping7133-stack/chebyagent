package com.chebysight.chebyagent.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Build;
import android.os.CancellationSignal;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

/** Human-only UI; it is deliberately not exported and has no automation setter. */
public final class CommerceAuthorizationActivity extends Activity {
    static final String EXTRA_REQUEST = "commerce_approval_request";
    private CancellationSignal authentication;
    private String requestId;
    private Switch mode;
    private boolean updatingMode;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        CommerceAuthorization.ownerInteractionActive = true;
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        requestId = getIntent().getStringExtra(EXTRA_REQUEST);
        ScrollView scroll = new ScrollView(this);
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        int pad = Math.round(24 * getResources().getDisplayMetrics().density);
        column.setPadding(pad, pad * 2, pad, pad);
        column.setBackgroundColor(Color.WHITE);
        column.setContentDescription("用户手动操作授权设置");
        column.setFilterTouchesWhenObscured(true);
        scroll.addView(column);
        setContentView(scroll);

        addText(column, "操作授权", 26);
        addText(column, "由你决定是否逐次确认。Agent 不能通过手机控制工具替你修改此设置。", 16);
        mode = new Switch(this);
        mode.setText("付款、下单免逐次确认");
        mode.setTextSize(18);
        mode.setChecked(CommerceAuthorization.isPreapproved(this));
        mode.setFilterTouchesWhenObscured(true);
        column.addView(mode);
        mode.setOnCheckedChangeListener((button, checked) -> {
            if (updatingMode) return;
            if (!checked) {
                if (!CommerceAuthorization.disableByOwner(this)) {
                    updateMode(CommerceAuthorization.isPreapproved(this));
                    notice("未能保存，请重试");
                }
                return;
            }
            updateMode(false);
            new AlertDialog.Builder(this)
                    .setTitle("开启免逐次确认？")
                    .setMessage("开启后，Agent 执行明确的付款、下单或预约操作时，不再逐次询问，可能产生真实扣款和订单。设置会在本机保存，你可以随时关闭。手机系统及第三方服务要求的验证仍然有效。")
                    .setNegativeButton("取消", null)
                    .setPositiveButton("由我授权开启", (dialog, which) -> authenticateOwner(true))
                    .show();
        });
        addText(column, "默认关闭：付款、下单前由你授权。\n手动开启：你预先授权此类操作，后续免逐次确认。\n普通浏览、搜索、筛选不需要这个授权。", 15);

        if (requestId != null) {
            CommerceAuthorization.Pending pending = CommerceAuthorization.get(requestId);
            if (pending == null) {
                addText(column, "这次授权请求已过期，请返回任务重新核对页面。", 16);
            } else {
                String app = pending.packageName;
                try {
                    app = getPackageManager().getApplicationLabel(
                            getPackageManager().getApplicationInfo(app, 0)).toString();
                } catch (Exception ignored) { }
                addText(column, "等待你确认", 21);
                addText(column, app + "\n操作：" + pending.label, 17);
                addText(column, "请核对原应用中的金额、收款方和订单。金额未自动核验；本次授权只绑定刚才的页面与点击目标，页面变化或两分钟后失效。", 15);
                Button approve = new Button(this);
                approve.setText("仅授权这一次");
                approve.setFilterTouchesWhenObscured(true);
                approve.setOnClickListener(v -> authenticateOwner(false));
                column.addView(approve);
            }
        }
        Button done = new Button(this);
        done.setText("返回");
        done.setOnClickListener(v -> finish());
        column.addView(done);
    }

    private void authenticateOwner(boolean enable) {
        try {
            BiometricPrompt.Builder builder = new BiometricPrompt.Builder(this)
                    .setTitle("确认操作授权")
                    .setDescription(enable ? "允许付款、下单免逐次确认，可随时关闭" : "仅允许刚才这一次操作");
            if (Build.VERSION.SDK_INT >= 30) {
                builder.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG
                        | BiometricManager.Authenticators.DEVICE_CREDENTIAL);
            } else {
                builder.setNegativeButton("取消", getMainExecutor(), (dialog, which) -> { });
            }
            if (authentication != null) authentication.cancel();
            authentication = new CancellationSignal();
            BiometricPrompt.CryptoObject crypto = new BiometricPrompt.CryptoObject(CommerceAuthorization.beginOwnerProof());
            builder.build().authenticate(crypto, authentication, getMainExecutor(),
                    new BiometricPrompt.AuthenticationCallback() {
                        @Override
                        public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                            if (isFinishing() || isDestroyed()) return;
                            try {
                                byte[] proof = CommerceAuthorization.finishOwnerProof(result.getCryptoObject().getSignature(),
                                        enable ? null : requestId);
                                if (enable) {
                                    boolean saved = CommerceAuthorization.saveOwnerProof(CommerceAuthorizationActivity.this, proof);
                                    updateMode(saved);
                                    notice(saved ? "已由你开启免逐次确认，可随时关闭" : "未能保存，请重试");
                                } else if (CommerceAuthorization.approveOnce(requestId)) {
                                    AuditLog.append(CommerceAuthorizationActivity.this, "owner_commerce_approved_once",
                                            JsonUtil.obj("approved", true));
                                    notice("已授权这一次；返回后可让 Agent 继续");
                                    finish();
                                } else {
                                    notice("请求已过期，请让 Agent 重新核对页面");
                                }
                            } catch (Exception failure) {
                                notice("未能验证或保存授权，设置未变更");
                            }
                        }

                        @Override
                        public void onAuthenticationError(int code, CharSequence message) {
                            if (!isFinishing() && !isDestroyed()) notice("身份验证未完成，未新增授权");
                        }
                    });
        } catch (Exception failure) {
            notice("需要系统支持的锁屏密码或生物识别验证；授权设置未变更");
        }
    }

    @Override
    protected void onDestroy() {
        if (authentication != null) authentication.cancel();
        if (!isChangingConfigurations()) CommerceAuthorization.ownerInteractionActive = false;
        super.onDestroy();
    }

    private void addText(LinearLayout column, String text, int size) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(size);
        view.setTextColor(Color.rgb(30, 35, 45));
        view.setPadding(0, 20, 0, 20);
        column.addView(view);
    }

    private void notice(String text) {
        Toast.makeText(this, text, Toast.LENGTH_LONG).show();
    }

    private void updateMode(boolean checked) {
        updatingMode = true;
        mode.setChecked(checked);
        updatingMode = false;
    }
}
