package com.everetro.poc;

import android.os.Bundle;
import android.widget.TextView;
import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

public class MainActivity extends AppCompatActivity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        TextView tv = findViewById(R.id.report);
        tv.setText("Running Python dependency checks…");

        // Spusť Python interpreter a zavolej poc.run_checks()
        new Thread(() -> {
            String report;
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(this));
                }
                Python py = Python.getInstance();
                PyObject mod = py.getModule("poc");
                report = mod.callAttr("run_checks").toString();
            } catch (Throwable t) {
                report = "Python startup failed:\n" + android.util.Log.getStackTraceString(t);
            }
            final String result = report;
            runOnUiThread(() -> tv.setText(result));
        }).start();
    }
}
