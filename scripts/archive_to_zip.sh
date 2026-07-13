#!/bin/bash
# Archive existing XML files in archive directory by date
# Groups files by YYYYMMDD prefix and creates YYYYMMDD.zip files

set -e

ARCHIVE_DIR="${1:-/volume1/docker/cel/archive}"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "=========================================="
echo "Archive XML Files to Daily Zip Files"
echo "=========================================="
echo ""
echo "Archive directory: $ARCHIVE_DIR"
echo ""

# Check if directory exists
if [ ! -d "$ARCHIVE_DIR" ]; then
    echo -e "${RED}Error: Archive directory does not exist: $ARCHIVE_DIR${NC}"
    exit 1
fi

# Check if we have write permission
if [ ! -w "$ARCHIVE_DIR" ]; then
    echo -e "${RED}Error: No write permission to archive directory${NC}"
    echo "Try running with sudo or as the correct user"
    exit 1
fi

# Count XML files
XML_COUNT=$(find "$ARCHIVE_DIR" -maxdepth 1 -name "*.xml" -type f | wc -l)

if [ "$XML_COUNT" -eq 0 ]; then
    echo -e "${YELLOW}No XML files found in archive directory${NC}"
    echo "Nothing to do."
    exit 0
fi

echo "Found $XML_COUNT XML files to archive"
echo ""

# Create temporary directory for grouping
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Extract unique dates from filenames (YYYYMMDD prefix)
echo "Analyzing file dates..."
DATES=$(find "$ARCHIVE_DIR" -maxdepth 1 -name "*.xml" -type f -printf "%f\n" | \
    grep -oE '^[0-9]{8}' | sort -u)

if [ -z "$DATES" ]; then
    echo -e "${RED}Error: No files with YYYYMMDD prefix found${NC}"
    exit 1
fi

DATE_COUNT=$(echo "$DATES" | wc -l)
echo "Found files for $DATE_COUNT unique dates"
echo ""

# Process each date
TOTAL_ZIPPED=0
TOTAL_FILES=0

for DATE in $DATES; do
    echo -e "${GREEN}Processing date: $DATE${NC}"

    ZIP_FILE="$ARCHIVE_DIR/${DATE}.zip"

    # Check if zip already exists
    if [ -f "$ZIP_FILE" ]; then
        echo -e "${YELLOW}  Zip file already exists: ${DATE}.zip${NC}"
        # Create with timestamp instead
        TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
        ZIP_FILE="$ARCHIVE_DIR/${DATE}_${TIMESTAMP}.zip"
        echo "  Using: ${DATE}_${TIMESTAMP}.zip"
    fi

    # Find all XML files for this date
    FILES=$(find "$ARCHIVE_DIR" -maxdepth 1 -name "${DATE}_*.xml" -type f)
    FILE_COUNT=$(echo "$FILES" | wc -l)

    if [ -z "$FILES" ]; then
        echo "  No files found for this date (skipping)"
        continue
    fi

    echo "  Found $FILE_COUNT files for this date"

    # Create zip file
    echo "  Creating zip file..."

    # Change to archive directory
    cd "$ARCHIVE_DIR" || { echo "Failed to cd to $ARCHIVE_DIR"; continue; }

    # Add files one by one (safer and shows progress)
    ADDED_COUNT=0
    for FILE in $FILES; do
        BASENAME=$(basename "$FILE")
        if [ -f "$BASENAME" ]; then
            zip -q "$ZIP_FILE" "$BASENAME"
            if [ $? -eq 0 ]; then
                ADDED_COUNT=$((ADDED_COUNT + 1))
            fi
        else
            echo "  Warning: File not found: $BASENAME"
        fi
    done

    echo "  Added $ADDED_COUNT files to zip"

    if [ $ADDED_COUNT -gt 0 ]; then
        echo -e "${GREEN}  ✓ Created: $(basename $ZIP_FILE)${NC}"

        # Check if zip file exists and has content
        if [ ! -s "$ZIP_FILE" ]; then
            echo -e "${RED}  ✗ Zip file is empty!${NC}"
            rm -f "$ZIP_FILE"
            continue
        fi

        # Get file size
        ZIP_SIZE=$(du -h "$ZIP_FILE" | cut -f1)
        echo "  Zip size: $ZIP_SIZE"

        # Sanity check: verify we actually added files
        if [ $ADDED_COUNT -gt 0 ] && [ -s "$ZIP_FILE" ]; then
            echo -e "${GREEN}  ✓ Zip file contains $ADDED_COUNT files (verified by creation)${NC}"

            # Delete original XML files
            echo "  Removing original XML files..."
            for FILE in $FILES; do
                rm -f "$FILE"
            done

            echo -e "${GREEN}  ✓ Removed $ADDED_COUNT XML files${NC}"

            TOTAL_ZIPPED=$((TOTAL_ZIPPED + 1))
            TOTAL_FILES=$((TOTAL_FILES + ADDED_COUNT))
        else
            echo -e "${RED}  ✗ No files were added to zip or zip is empty!${NC}"
            echo "  Keeping original XML files for safety"
            rm -f "$ZIP_FILE"
        fi
    else
        echo -e "${RED}  ✗ Failed to create zip file${NC}"
    fi

    echo ""
done

# Summary
echo "=========================================="
echo "Summary"
echo "=========================================="
echo "Created zip files: $TOTAL_ZIPPED"
echo "Archived XML files: $TOTAL_FILES"
echo ""

# Show remaining files
REMAINING_XML=$(find "$ARCHIVE_DIR" -maxdepth 1 -name "*.xml" -type f | wc -l)
TOTAL_ZIPS=$(find "$ARCHIVE_DIR" -maxdepth 1 -name "*.zip" -type f | wc -l)

echo "Archive directory contents:"
echo "  ZIP files: $TOTAL_ZIPS"
echo "  XML files: $REMAINING_XML"

if [ "$REMAINING_XML" -gt 0 ]; then
    echo ""
    echo -e "${YELLOW}Note: $REMAINING_XML XML files remain (may not have YYYYMMDD prefix)${NC}"
fi

echo ""
echo -e "${GREEN}Done!${NC}"
