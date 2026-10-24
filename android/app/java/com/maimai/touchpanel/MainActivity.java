package com.maimai.touchpanel;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

/**
 * Thin shell around the maimai_pre_touch web client.
 *
 * The page itself lives on the PC server so it can be updated without rebuilding
 * the APK; this activity only provides fullscreen/immersive display, a screen
 * wake lock, WebRTC friendly WebView settings and a way to (re)enter the PC's
 * address when the server cannot be reached.
 */
public class MainActivity extends Activity {
    private static final String TAG = "maimai_pre_touch";
    private static final String PREFS = "maimai_pre_touch";
    private static final String KEY_SERVER = "server";
    private static final String DEFAULT_SERVER = "192.168.31.27:8770";

    private WebView web;
    private LinearLayout root;
    private String server;
    private long lastBackPress = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        enterImmersive();

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        root.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        SharedPreferences prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String fromIntent = getIntent() != null ? getIntent().getStringExtra("server") : null;
        server = fromIntent != null && !fromIntent.isEmpty()
                ? fromIntent
                : prefs.getString(KEY_SERVER, null);

        if (server == null || server.isEmpty()) {
            askServer("请填写运行 PC 服务端的局域网地址\n（例如 192.168.31.27:8770）", DEFAULT_SERVER);
        } else {
            loadServer(server);
        }
    }

    private void enterImmersive() {
        View decor = getWindow().getDecorView();
        decor.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    private void createWebView() {
        if (web != null) {
            return;
        }
        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        web.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setAllowFileAccess(false);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }
        web.setLayerType(View.LAYER_TYPE_HARDWARE, null);
        web.setKeepScreenOn(true);

        WebView.setWebContentsDebuggingEnabled(true);

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage message) {
                Log.i(TAG, "console: " + message.message() + " @" + message.lineNumber());
                return true;
            }
        });

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    showConnectionProblem("无法连接到 " + server + "\n" + error.getDescription());
                }
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
                showConnectionProblem("无法连接到 " + server + "\n" + description);
            }
        });

        root.addView(web, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private String normalise(String raw) {
        String value = raw.trim();
        if (value.isEmpty()) {
            return DEFAULT_SERVER;
        }
        value = value.replace("http://", "").replace("https://", "");
        if (value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        if (!value.contains(":")) {
            value = value + ":8770";
        }
        return value;
    }

    private void loadServer(String address) {
        server = normalise(address);
        getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_SERVER, server)
                .apply();
        if (root.getChildCount() > 0) {
            root.removeAllViews();
        }
        createWebView();
        String url = "http://" + server + "/app/";
        Log.i(TAG, "loading " + url);
        web.loadUrl(url);
        web.requestFocus();
    }

    private void showConnectionProblem(String message) {
        root.removeAllViews();
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(48, 96, 48, 48);
        box.setBackgroundColor(Color.BLACK);

        TextView text = new TextView(this);
        text.setText(message + "\n\n请确认 PC 端已运行 pc\\server.py，且手机与 PC 在同一局域网。");
        text.setTextColor(Color.WHITE);
        text.setTextSize(16);
        box.addView(text);

        Button retry = new Button(this);
        retry.setText("重试");
        retry.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                loadServer(server);
            }
        });
        box.addView(retry);

        Button change = new Button(this);
        change.setText("修改服务器地址");
        change.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                askServer("填写 PC 服务端地址", server);
            }
        });
        box.addView(change);

        root.addView(box);
    }

    private void askServer(String message, String initial) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setText(initial == null ? DEFAULT_SERVER : initial);
        input.setSelection(input.getText().length());

        new AlertDialog.Builder(this)
                .setTitle("服务器地址")
                .setMessage(message)
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("连接", (dialog, which) -> loadServer(input.getText().toString()))
                .show();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            enterImmersive();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) {
            web.onPause();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        enterImmersive();
        if (web != null) {
            web.onResume();
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (event.getRepeatCount() == 0) {
                event.startTracking();
                return true;
            }
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public boolean onKeyLongPress(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            askServer("填写 PC 服务端地址", server);
            return true;
        }
        return super.onKeyLongPress(keyCode, event);
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (event.isTracking() && !event.isCanceled()) {
                long now = System.currentTimeMillis();
                if (now - lastBackPress < 2000) {
                    finish();
                } else {
                    lastBackPress = now;
                    Toast.makeText(this, "再按一次返回键退出（长按可修改服务器地址）", Toast.LENGTH_SHORT).show();
                }
            }
            return true;
        }
        return super.onKeyUp(keyCode, event);
    }
}
