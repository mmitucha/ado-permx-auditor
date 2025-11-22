#!/bin/bash

# Azure DevOps Permissions Auditor - Run Script (using uv)
# This script helps you run the auditor with uv

echo "Azure DevOps Permissions Auditor"
echo "================================="
echo ""

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "❌ uv is not installed"
    echo ""
    echo "Install uv with:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    exit 1
fi

echo "✓ uv is installed"
echo ""

# Check for .env file
if [ ! -f .env ]; then
    echo "⚠️  .env file not found"
    echo ""
    echo "Create a .env file with:"
    echo "  ADO_ORGANIZATION=your-org-name"
    echo "  ADO_PAT_TOKEN=your-pat-token"
    echo ""
    exit 1
fi

# Load environment variables
echo "Loading environment variables from .env..."
set -a
source .env
set +a

# Verify required variables
if [ -z "$ADO_ORGANIZATION" ]; then
    echo "❌ ADO_ORGANIZATION not set in .env"
    exit 1
fi

if [ -z "$ADO_PAT_TOKEN" ]; then
    echo "❌ ADO_PAT_TOKEN not set in .env"
    exit 1
fi

echo "✓ Organization: $ADO_ORGANIZATION"
echo "✓ PAT token is configured"
echo ""

# Confirm before running
read -p "Start audit now? (y/n): " start_now

if [ "$start_now" = "y" ] || [ "$start_now" = "Y" ]; then
    echo ""
    echo "Starting audit..."
    echo "This may take several hours for large organizations."
    echo "Progress will be logged to console and log file."
    echo ""

    uv run ado_permissions_auditor.py
    exit_code=$?

    # Check if audit completed successfully
    if [ $exit_code -eq 0 ]; then
        echo ""
        echo "✓ Audit completed successfully!"
        echo ""
        echo "Output files:"
        ls -lh audit_output/ado_permissions_audit_*.csv 2>/dev/null | tail -1
        ls -lh audit_output/ado_audit_*.log 2>/dev/null | tail -1

        # Ask if user wants to analyze results
        echo ""
        read -p "Would you like to analyze the results? (y/n): " analyze_now

        if [ "$analyze_now" = "y" ] || [ "$analyze_now" = "Y" ]; then
            latest_csv=$(ls -t audit_output/ado_permissions_audit_*.csv 2>/dev/null | head -1)
            if [ -n "$latest_csv" ]; then
                echo "Analyzing $latest_csv..."
                uv run analyze_permissions.py "$latest_csv" "audit_output/analysis_report.json"
            fi
        fi
    else
        echo ""
        echo "❌ Audit failed with exit code $exit_code"
        echo ""

        # Show recent log file for context
        latest_log=$(ls -t audit_output/ado_audit_*.log 2>/dev/null | head -1)
        if [ -n "$latest_log" ]; then
            echo "Recent errors from $latest_log:"
            echo "---"
            grep -E "ERROR|CRITICAL" "$latest_log" | tail -10
            echo "---"
            echo ""
            echo "Full log: $latest_log"
        fi

        echo ""
        echo "Common issues:"
        echo "  • 401 Unauthorized: Check your PAT token has correct permissions"
        echo "  • Wrong account type: Ensure PAT is from work/school account, not personal"
        echo "  • Organization name: Verify ADO_ORGANIZATION matches your Azure DevOps URL"
        echo ""
        exit 1
    fi
else
    echo ""
    echo "Audit cancelled. You can run it manually with:"
    echo "  export \$(cat .env | grep -v '^#' | xargs) && uv run ado_permissions_auditor.py"
fi

echo ""
echo "Done!"
