# Create your views here.
from datetime import datetime

from django.shortcuts import render
from django.http import JsonResponse, Http404, FileResponse, HttpResponse
from django.core.files.storage import FileSystemStorage
from django.views.decorators.csrf import csrf_exempt
from .ml.main import run_forecast
from .ml.utils.setup_logging import setup_logging
from .ml.enums import ForecastType

import plotly.io as pio
import json
import os
import pandas as pd
import traceback
from pathlib import Path

logger = setup_logging()


def upload_file(request):
    """Renders the upload form and handles forecast display."""
    if request.method == "POST" and request.FILES.get("dataset"):

        # Clear any old session data to avoid stale suggestions
        for key in ["csv_base_filename", "uploaded_file_path"]:
            if key in request.session:
                del request.session[key]

        file = request.FILES["dataset"]
        fs = FileSystemStorage()
        filename = fs.save(file.name, file)
        file_path = fs.path(filename)
        logger.info(f"DEBUG select filename from POST: {filename}")
        logger.info(f"DEBUG select file path from POST: {file_path}")

        # Make sure it's stored in session right away
        request.session["csv_base_filename"] = filename
        request.session["uploaded_file_path"] = file_path
        request.session.modified = True

        # Safely convert to enum
        selected_type = request.POST.get("forecast_type", "monthly")
        logger.info(f"DEBUG selected_type from POST: {selected_type}")
        try:
            forecast_type = ForecastType(selected_type)

        except ValueError:
            forecast_type = ForecastType.MONTHLY

        # Optional account name input
        account_name = request.POST.get("account_name", "").strip()

        # Optional service name input
        service_name = request.POST.get("service_name", "").strip()

        # Optional bu code input
        bu_code_raw = request.POST.get("bu_code", "").strip()

        if bu_code_raw == "":
            bu_code = None
        else:
            bu_code = int(bu_code_raw)  # will only run when non-empty

        # Optional segment name input
        segment_name = request.POST.get("segment_name", "").strip()

        try:
            logger.info(f"Running forecast with type: {forecast_type}")
            forecast_type_str = forecast_type.value if hasattr(forecast_type, "value") else str(forecast_type)

            # "Method overloading" behavior via kwargs
            kwargs = {}

            # only include account_name if relevant
            if forecast_type == ForecastType.ACCOUNT and account_name:
                kwargs["account_name"] = account_name

            if forecast_type == ForecastType.SERVICE:
                if service_name:
                    kwargs["service_name"] = service_name
                if account_name:
                    kwargs["account_name"] = account_name

            if forecast_type == ForecastType.BUCODE:
                if bu_code is not None:
                    kwargs["bu_code"] = bu_code

            if forecast_type == ForecastType.SEGMENT:
                if segment_name:
                    kwargs["segment_name"] = segment_name
                if service_name:
                    kwargs["service_name"] = service_name
                if account_name:
                    kwargs["account_name"] = account_name

            result = run_forecast(file_path, forecast_type, **kwargs)

            forecast_df = result["forecast"]
            historical_df = result["history"]


            # Only keep ds, yhat, yhat_lower, yhat_upper, and accountName columns for download
            if not isinstance(forecast_df, pd.DataFrame):
                forecast_df = pd.DataFrame(forecast_df)

            possible_column_names = get_dynamic_column_names(forecast_df)
            ACCOUNT_COL = possible_column_names.get("accountName")
            SERVICE_COL = possible_column_names.get("serviceName")
            COST_COL = possible_column_names.get("cost")
            MONTH_COL = possible_column_names.get("month")

            # Restrict columns
            keep_cols = ["ds", "yhat", "yhat_lower", "yhat_upper"]
            forecast_df = forecast_df[[col for col in keep_cols if col in forecast_df.columns]]

            # Convert forecast dataframe to JSON for Chart.js + download
            forecast_json = forecast_df.to_json(orient="records", date_format="iso")
            historical_json = historical_df.to_json(orient="records", date_format="iso")

            logger.info("Successfully converted forecast data for Chart.js")

            # Store the forecast data in session for later CSV download
            request.session['forecast_csv_json'] = forecast_json

            request.session['csv_base_filename'] = filename  # original uploaded file name
            request.session['forecast_type'] = forecast_type.value if hasattr(forecast_type, "value") else str(forecast_type)
            request.session['account_name'] = account_name
            request.session['service_name'] = service_name
            request.session['bu_code'] = bu_code
            request.session['segment_name'] = segment_name

            logger.info(f"DEBUG result keys: {list(result.keys())}")

            return render(request, "forecast/dashboard.html", {
                "forecast_data": forecast_json,
                "historical_data": historical_json,
                "forecast_type": forecast_type.value if hasattr(forecast_type, "value") else forecast_type,
                "account_name": account_name if forecast_type in [ForecastType.ACCOUNT, ForecastType.SERVICE, ForecastType.SEGMENT] else None,
                "service_name": service_name if forecast_type in [ForecastType.SERVICE, ForecastType.SEGMENT] else None,
                "bu_code": bu_code if forecast_type == ForecastType.BUCODE else None,
                "segment_name": segment_name if forecast_type == ForecastType.SEGMENT else None,
            })
        except Exception as e:
            logger.error(f"Forecasting failed in views: {e}")
            return render(request, "forecast/upload.html", {"error": str(e)})

    return render(request, "forecast/upload.html")


def get_suggestions(request):
    query = request.GET.get("q", "").strip().lower()
    field = request.GET.get("field")  # 'account', 'service', 'bucode', 'segment'
    logger.info(f"Received suggestion request: field={field}, query='{query}'")

    #To Do: Fix the file path when upload_file button is not clicked.

    filename = request.session.get("csv_base_filename")

    logger.info(f"DEBUG select filename from POST: {filename}")
    

    if not filename:
        print("❌ No file found in session — likely no upload in this session.")
        # Try to get the most recently uploaded file (as fallback)
        fs = FileSystemStorage()
        files = sorted(fs.listdir(fs.location)[1], key=lambda f: os.path.getctime(os.path.join(fs.location, f)),
                       reverse=True)
        if files:
            filename = files[0]
            print(f"⚠️ Using fallback file: {filename}")
            request.session["csv_base_filename"] = filename
        else:
            return JsonResponse({"suggestions": []})

    fs = FileSystemStorage()
    file_path = fs.path(filename)
    logger.info(f"DEBUG select file_path from POST: {file_path}")
    
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return JsonResponse({"suggestions": []})

    try:
        df = pd.read_csv(file_path)
        print(f"✅ Loaded file with columns: {list(df.columns)}")
        print(f"✅ File path for loading suggestions: {file_path}")
        # possible_account_names_column = ["Account Name", "vendor_account_name", "accountName"]
        # current_account_name = None
        # for col in possible_account_names_column:
        #     if col in df.columns:
        #         current_account_name = col
        #         break
        # print(f"✅ Loaded file with account name column: {current_account_name}")

        # current_service_name = None
        # for col in df.columns:
        #     if "service" in col.lower():
        #         current_service_name = col
        #         break

        possible_column_names = get_dynamic_column_names(df)

        if field == "account":
            col_name = possible_column_names.get("accountName")
        elif field == "service":
            col_name = possible_column_names.get("serviceName")
        elif field == "bu_code":
            col_name = "buCode"
        elif field == "segment":
            col_name = "segment"
        else:
            return JsonResponse({"error": "Invalid field"}, status=400)

        if col_name not in df.columns:
            print(f"❌ Column {col_name} not found in CSV.")
            return JsonResponse({"suggestions": []})

        unique_vals = df[col_name].dropna().unique().tolist()
        matches = [v for v in unique_vals if query in str(v).lower()]
        print(f"🔍 Query='{query}' found {len(matches)} matches: {matches[:5]}")
        print(f"🧪 First 5 {current_account_name} values: {df[current_account_name].dropna().unique()[:5]}")
        print(f"🧪 First 5 {current_service_name} values: {df[current_service_name].dropna().unique()[:5]}")

        return JsonResponse({"suggestions": matches[:10]})

    except Exception as e:
        print(f"ERROR in get_suggestions: {e}")
        return JsonResponse({"error": str(e)}, status=500)


def download_forecast_csv(request):
    """Return the forecast CSV stored in session as a downloadable file."""
    csv_data = request.session.get("forecast_csv_json")

    # if there's nothing in session we can't proceed
    if not csv_data:
        return HttpResponse("No forecast data available. Please generate a forecast first.", status=404)


    # load the dataframe and restrict to only ds, yhat, yhat_lower, yhat_upper, accountName
    forecast_df = pd.read_json(csv_data, orient="records")
    account_name = request.session.get("account_name", "")
    if "accountName" not in forecast_df.columns:
        forecast_df["accountName"] = account_name or None
    else:
        forecast_df["accountName"] = account_name or None
    keep_cols = ["ds", "yhat", "yhat_lower", "yhat_upper", "accountName"]
    forecast_df = forecast_df[[col for col in keep_cols if col in forecast_df.columns]]
    csv_string = forecast_df.to_csv(index=False)

    base_filename = request.session.get("csv_base_filename", "forecast")
    account_name = request.session.get("account_name", "")
    service_name = request.session.get("service_name", "")
    forecast_type = request.session.get("forecast_type", "monthly")

    # Strip the .csv extension if present
    # strip any path components that might accidentally be included
    base_filename = os.path.basename(base_filename)
    base_filename = os.path.splitext(base_filename)[0]

    # Clean up names (remove spaces, lower-case)
    def clean(name):
        return name.strip().replace(" ", "_").lower() if name else ""

    account_name = clean(account_name)
    service_name = clean(service_name)

    # Construct filename based on conditions
    if account_name and service_name:
        filename = f"{base_filename}-forecasts-{account_name}-{service_name}.csv"
    elif account_name:
        filename = f"{base_filename}-forecasts-{account_name}.csv"
    else:
        filename = f"{base_filename}-forecasts-{forecast_type}-aggregate.csv"

    response = HttpResponse(csv_string, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def dashboard_view(request):
    # Suppose this is your forecasts DataFrame
    forecasts_formatted = request['forecast']
    metrics = request['metrics']

    # Convert to JSON
    chart_data = forecasts_formatted.to_dict(orient='records')
    chart_json = json.dumps(chart_data, default=str)

    return render(request, "dashboard.html", {
        "chart_json": chart_json,
        "metrics": metrics,
    })


@csrf_exempt
def forecast_api(request):
    """
    API endpoint for programmatic access.
    POST a CSV file → returns JSON with forecast + metrics + figure JSON.
    """
    if request.method == "POST" and request.FILES.get("dataset"):
        file = request.FILES["dataset"]
        fs = FileSystemStorage()
        filename = fs.save(file.name, file)
        file_path = fs.path(filename)
        forecast_type = request.POST.get("forecast_type", "monthly")  # default to monthly

        try:
            # Normalize forecast_type to enum if possible
            try:
                forecast_type_enum = ForecastType(forecast_type)
            except Exception:
                forecast_type_enum = ForecastType.MONTHLY

            result = run_forecast(file_path, forecast_type_enum)

            # --- NEW: ensure meta columns in API response as well ---
            forecast_df = result.get("forecast")
            if not isinstance(forecast_df, pd.DataFrame):
                forecast_df = pd.DataFrame(forecast_df)

            ACCOUNT_COL = "accountName"
            SERVICE_COL = "serviceName"
            SEGMENT_COL = "segment"
            BUCODE_COL = "buCode"
            TYPE_COL = "forecast_type"

            # Allow API callers to optionally pass these too
            account_name = (request.POST.get("account_name") or "").strip() or None
            service_name = (request.POST.get("service_name") or "").strip() or None
            segment_name = (request.POST.get("segment_name") or "").strip() or None
            bu_code_val = (request.POST.get("bu_code") or "").strip()
            bu_code = int(bu_code_val) if bu_code_val.isdigit() else None

            meta_values = {
                ACCOUNT_COL: account_name,
                SERVICE_COL: service_name,
                SEGMENT_COL: segment_name,
                BUCODE_COL: bu_code,
                TYPE_COL: forecast_type_enum.value if hasattr(forecast_type_enum, "value") else str(forecast_type_enum),
            }

            for col, val in meta_values.items():
                if col not in forecast_df.columns:
                    forecast_df[col] = val
                else:
                    forecast_df[col] = forecast_df[col].fillna(val)

            ordered_first = ["ds", "yhat", "yhat_lower", "yhat_upper", TYPE_COL, ACCOUNT_COL, SERVICE_COL, SEGMENT_COL, BUCODE_COL]
            existing = [c for c in ordered_first if c in forecast_df.columns]
            rest = [c for c in forecast_df.columns if c not in existing]
            forecast_df = forecast_df[existing + rest]

            return JsonResponse({
                "status": "success",
                "metrics": result.get("metrics"),
                "figure_json": result.get("figure_json"),
                # Return records so API consumers get the extra columns
                "forecast": json.loads(forecast_df.to_json(orient="records", date_format="iso")),
            })
        except Exception as e:
            logger.error(f"Forecasting failed in API: {e}")
            return JsonResponse({"status": "error", "message": str(e)}, status=500)

    return JsonResponse({"status": "error", "message": "No dataset uploaded"}, status=400)

def get_dynamic_column_names(df):
    """
    # Implementation for getting dynamic column names
    """
    dynamic_column_names = {}


    possible_account_names_column = ["Account Name", "vendor_account_name", "accountName"]
    current_account_name = None
    for col in possible_account_names_column:
        if col in df.columns:
            current_account_name = col
            break
    print(f"✅ Loaded file with account name column: {current_account_name}")
    dynamic_column_names["accountName"] = current_account_name
    if not current_account_name:
        raise ValueError("No account name column found in CSV for account-level forecast.")

    current_service_name = None
    for col in df.columns:
        if "service" in col.lower():
            current_service_name = col
            break
    print(f"✅ Loaded file with service name column: {current_service_name}")
    dynamic_column_names["serviceName"] = current_service_name
    if not current_service_name:
        raise ValueError("No service name column found in CSV for service-level forecast.")

    current_cost_name = None
    for col in df.columns:
        if "cost" in col.lower():
            current_cost_name = col
            break
    print(f"✅ Loaded file with cost name column: {current_cost_name}")
    dynamic_column_names["cost"] = current_cost_name
    if not current_cost_name:
        raise ValueError("No cost name column found in CSV for cost-level forecast.")

    possible_month_names_column = ["Month", "Month(Year)"]
    current_month_name = None
    for col in possible_month_names_column:
        if col in df.columns:
            current_month_name = col
            break
    print(f"✅ Loaded file with month name column: {current_month_name}")
    dynamic_column_names["month"] = current_month_name
    if not current_month_name:
        raise ValueError("No month name column found in CSV for month-level forecast.")

    logger.info(f"Final dynamic column names: {dynamic_column_names}")
    
    return dynamic_column_names
